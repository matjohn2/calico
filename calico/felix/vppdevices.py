# -*- coding: utf-8 -*-
# Copyright (c) 2014-2016 Tigera, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
felix.devices
~~~~~~~~~~~~

Utility functions for managing devices in Felix.
"""
import abc
import logging
import re
import os
import socket
import struct
from collections import defaultdict

from netaddr import IPAddress, EUI, mac_bare

from calico import common
from calico.felix.actor import Actor, actor_message
from calico.felix import futils
from calico.felix.fplugin import FelixPlugin
from calico.felix.futils import FailedSystemCall
from calico.felix.devices import DevicesPlugin

# VPP API. Currently a manual dependancy (no pypy module)
import vpp_papi

# Logger
_log = logging.getLogger(__name__)

class VppDevices(DevicesPlugin):

    # TODO. We create interfaces in different functions than adding IP's and routes.
    # For now. Lets keep the ifindex to calico ifname in a dict. (Class attribute).
    vpp_resolve_if_index = dict()
    #Usage VppDevices.vpp_resolve_if_index

    #TEMPORARY. Probably a nicer way to do this
    #Usually, the calling binary (calicoctl/CNI) is responsible for container to host link local configuration.
    #However, we need our VPP instance to have a link local IP for the container<>VPP Interface.
    #Here we are taking the MAC used INSIDE THE CONTAINER (Calico gives us this in set_routes) and modifying by a couple of bits (low1 and low2 should be 0xff and 0xffff if you wanted an actual MAC<>v6 conversion).
    #This means we'll get a slightly different IP than the one assigned to the container, in the same fd80 link local /64. So communication should work :)

    def mac2vpplinklocal(self, mac):

        # Remove the most common delimiters; dots, dashes, etc.
        mac_value = int(mac.translate(None, ' .:-'), 16)

        # Split out the bytes that slot into the IPv6 address
        # XOR the most significant byte with 0x02, inverting the
        # Universal / Local bit
        high2 = mac_value >> 32 & 0xffff ^ 0x0200
        high1 = mac_value >> 24 & 0xff
        low1 = mac_value >> 16 & 0xf0
        low2 = mac_value & 0xfff0

        return 'fe80::{:04x}:{:02x}ff:fe{:02x}:{:04x}'.format(
            high2, high1, low1, low2)

    def mac2linklocal(self, mac):

        # Remove the most common delimiters; dots, dashes, etc.
        mac_value = int(mac.translate(None, ' .:-'), 16)

        # Split out the bytes that slot into the IPv6 address
        # XOR the most significant byte with 0x02, inverting the
        # Universal / Local bit
        high2 = mac_value >> 32 & 0xffff ^ 0x0200
        high1 = mac_value >> 24 & 0xff
        low1 = mac_value >> 16 & 0xff
        low2 = mac_value & 0xffff

        return 'fe80::{:04x}:{:02x}ff:fe{:02x}:{:04x}'.format(
            high2, high1, low1, low2)

    def do_global_configuration(self):
        """
        Configures the global kernel config.  In particular, sets the flags
        that we rely on to ensure security, such as the kernel's RPF check.

        :raises BadKernelConfig if a problem is detected.
        """

        # For IPv4, we rely on the kernel's reverse path filtering to prevent
        # workloads from spoofing their IP addresses.
        #
        # The RPF check for a particular interface is controlled by several
        # sysctls:
        #
        # - ipv4.conf.all.rp_filter is a global override
        # - ipv4.conf.default.rp_filter controls the value that is set on a
        #   newly created interface
        # - ipv4.conf.<interface>.rp_filter controls a particular interface.
        #
        # The algorithm for combining the global override and per-interface
        # values is to take the *numeric* maximum between the two.  The values
        # are: 0=off, 1=strict, 2=loose.  "loose" is not suitable for Calico
        # since it would allow workloads to spoof packets from other workloads
        # on the same host.  Hence, we need the global override to be <=1 or
        # it would override the per-interface setting to "strict" that we
        # require.
        #
        # We bail out rather than simply setting it because setting 2, "loose",
        # is unusual and it is likely to have been set deliberately.
        _log.info("Using VPP Devices Plugin")
        #VPPSTART
        r = vpp_papi.connect("calicoctl")
        if r != 0:
            _log.critical("vppapi: could not connect to vpp")
        dst_address = "169.254.1.1".encode('utf-8', 'ignore')
        dst_address = socket.inet_pton(socket.AF_INET, dst_address)
        proxyarp_r = vpp_papi.proxy_arp_add_del(0, True, dst_address, dst_address)
        if type(proxyarp_r) == list:
            _log.critical("vppapi: addition of proxy arp for default gateway failed")
            return
        _log.debug("vppapi: added proxy for default gateway 169.254.1.1")
        #VPPEND
        ps_name = "/proc/sys/net/ipv4/conf/all/rp_filter"
        rp_filter = int(_read_proc_sys(ps_name))
        if rp_filter > 1:
            _log.critical("Kernel's RPF check is set to 'loose'.  This would "
                          "allow endpoints to spoof their IP address.  Calico "
                          "requires net.ipv4.conf.all.rp_filter to be set to "
                          "0 or 1.")
            raise BadKernelConfig("net.ipv4.conf.all.rp_filter set to 'loose'")

        # Make sure the default for new interfaces is set to strict checking so
        # that there's no race when a new interface is added and felix hasn't
        # configured it yet.
        _write_proc_sys("/proc/sys/net/ipv4/conf/default/rp_filter", "1")

        # We use sysfs for inspecting devices.
        if not os.path.exists("/sys/class/net"):
            raise BadKernelConfig("Felix requires sysfs to be mounted at /sys")

    def interface_exists(self, interface):
        """
        Checks if an interface exists.
        :param str interface: Interface name
        :returns: True if interface device exists

        Note: this checks that the interface exists at a particular point in
        time but the caller needs to be defensive to the interface
        disappearing before it has a chance to access it.
        """
        return os.path.exists("/sys/class/net/%s" % interface)

    def list_interface_ips(self, ip_type, interface):
        """
        List the local IPs assigned to an interface.
        :param str ip_type: IP type, either futils.IPV4 or futils.IPV6
        :param str interface: Interface name
        :returns: a set of all addresses directly assigned to the device.
        """
        assert ip_type in (futils.IPV4, futils.IPV6), (
            "Expected an IP type, got %s" % ip_type
        )
        if ip_type == futils.IPV4:
            data = futils.check_call(
                ["ip", "addr", "list", "dev", interface]).stdout
            regex = r'^    inet ([0-9.]+)'
        else:
            data = futils.check_call(
                ["ip", "-6", "addr", "list", "dev", interface]).stdout
            regex = r'^    inet6 ([0-9a-fA-F:.]+)'
        # Search the output for lines beginning "    inet(6)".
        ips = re.findall(regex, data, re.MULTILINE)
        _log.debug("Interface %s has %s IPs %s", interface, ip_type, ips)
        return set(IPAddress(ip) for ip in ips)

    def list_ips_by_iface(self, ip_type):
        """
        List the local IPs assigned to all interfaces.
        :param str ip_type: IP type, either futils.IPV4 or futils.IPV6
        :returns: a set of all addresses directly assigned to the device.
        """
        assert ip_type in (futils.IPV4, futils.IPV6), (
            "Expected an IP type, got %s" % ip_type
        )
        if ip_type == futils.IPV4:
            data = futils.check_call(["ip", "-4", "addr", "list"]).stdout
            regex = r'^    inet ([0-9.]+)'
        else:
            data = futils.check_call(["ip", "-6", "addr", "list"]).stdout
            regex = r'^    inet6 ([0-9a-fA-F:.]+)'

        ips_by_iface = defaultdict(set)
        iface_name = None
        for line in data.splitlines():
            m = re.match(r"^\d+: ([^:]+):", line)
            if m:
                iface_name = m.group(1)
            else:
                assert iface_name
                m = re.match(regex, line)
                if m:
                    ip = IPAddress(m.group(1))
                    ips_by_iface[iface_name].add(ip)
        return ips_by_iface

    def set_interface_ips(self, ip_type, interface, ips):
        """
        Set the IPs directly assigned to an interface.  Idempotent: does not
        flap addresses if they're already in place.

        :param str ip_type: IP type, either futils.IPV4 or futils.IPV6
        :param str interface: Interface name
        :param set[IPAddress] ips: The IPs to set or an empty set to remove all
               IPs.
        """
        assert ip_type in (futils.IPV4, futils.IPV6), (
            "Expected an IP type, got %s" % ip_type
        )
        old_ips = self.list_interface_ips(ip_type, interface)
        ips_to_add = ips - old_ips
        ips_to_remove = old_ips - ips
        ip_cmd = ["ip", "-6"] if ip_type == futils.IPV6 else ["ip"]
        for ip in ips_to_remove:
            _log.info("Removing IP %s from interface %s", ip, interface)
            futils.check_call(ip_cmd + ["addr", "del", str(ip), "dev",
                                        interface])
        for ip in ips_to_add:
            _log.info("Adding IP %s to interface %s", ip, interface)
            futils.check_call(ip_cmd + ["addr", "add", str(ip), "dev",
                                        interface])

    def list_interface_route_ips(self, ip_type, interface):
        """
        List IP addresses for which there are routes to a given interface.
        :param str ip_type: IP type, either futils.IPV4 or futils.IPV6
        :param str interface: Interface name
        :returns: a set of all addresses for which there is a route to the device.
        """
        ips = set()

        if ip_type == futils.IPV4:
            data = futils.check_call(
                ["ip", "route", "list", "dev", interface]).stdout
        else:
            data = futils.check_call(
                ["ip", "-6", "route", "list", "dev", interface]).stdout

        lines = data.split("\n")

        _log.debug("Existing routes to %s : %s", interface, lines)

        for line in lines:
            # Example of the lines we care about is (having specified the
            # device above):  "10.11.2.66 proto static scope link"
            words = line.split()

            if len(words) > 1:
                ip = words[0]
                if common.validate_ip_addr(ip,
                                           futils.IP_TYPE_TO_VERSION[ip_type]):
                    # Looks like an IP address. Note that we here are ignoring
                    # routes to networks configured when the interface is
                    # created.
                    ips.add(words[0])

        _log.debug("Found existing IP addresses : %s", ips)

        return ips

    def configure_interface_ipv4(self, if_name):
        """
        Configure the various proc file system parameters for the interface for
        IPv4.

        Specifically,
          - Allow packets from controlled interfaces to be directed to localhost
          - Enable proxy ARP
          - Enable the kernel's RPF check.

        :param if_name: The name of the interface to configure.
        :returns: None
        """
        # VPPSTART
        # Here we add the AF_PACKET interface into VPP.
        # Assuming for now that if we've created it once, do nothing next time. VPPTODO
        vpp_inter = if_name.encode('utf-8', 'ignore')
        _log.debug("vppapi: Processing v4 Creation of vpp_inter %s of type %s",
                   vpp_inter, type(vpp_inter))

        # If we already have this interface. Do nothing.
        if if_name in VppDevices.vpp_resolve_if_index:
            _log.debug("vppapi: We already have the calico interace assigned to an if_index: %s name: %s",
                        VppDevices.vpp_resolve_if_index[if_name], if_name)
            return
        _log.debug("VPPDICT before addition: %s", VppDevices.vpp_resolve_if_index)

        afp_r = vpp_papi.af_packet_create(vpp_inter,
                                 "00:00:00:00:00:00", True, False)
        if type(afp_r) != list and afp_r.retval == 0:
            sw_if_index = afp_r.sw_if_index
            admin_up_down = 1
            link_up = 1
            deleted = 0

            # Store the VPP sw_if_index to calico if_name mapping.
            VppDevices.vpp_resolve_if_index[if_name] = sw_if_index

            _log.debug("Associated calico interface %s with vpp if_index %s",
                       if_name, VppDevices.vpp_resolve_if_index[if_name])
            _log.debug("VPPDICT After addition of %s: %s",if_name, VppDevices.vpp_resolve_if_index)
            # If create success. Set interface flags to up.
            flags_r = vpp_papi.sw_interface_set_flags(sw_if_index,
                                                      admin_up_down,
                                                      link_up, deleted)

            if type(flags_r) == list or flags_r.retval != 0:
                _log.critical("vppapi: Call to set VPP interface flags"
                              "for %s failed???", vpp_inter)
                return

            _log.debug("vppapi: VPP AFP for %s created", vpp_inter)

        else:
            _log.debug("vppapi: v4_configure: could not create AFP: %s", vpp_inter)

        # Enable the kernel's RPF check, which ensures that a VM cannot spoof
        # its IP address.
        _write_proc_sys('/proc/sys/net/ipv4/conf/%s/rp_filter' % if_name, 1)
        _write_proc_sys('/proc/sys/net/ipv4/conf/%s/route_localnet' % if_name,
                        1)
        #_write_proc_sys("/proc/sys/net/ipv4/conf/%s/proxy_arp" % if_name, 1)
        #_write_proc_sys("/proc/sys/net/ipv4/neigh/%s/proxy_delay" % if_name, 0)

    def configure_interface_ipv6(self, if_name, proxy_target):
        """
        Configure an interface to support IPv6 traffic from an endpoint.
          - Enable proxy NDP on the interface.
          - Program the given proxy target (gateway the endpoint will use).

        :param if_name: The name of the interface to configure.
        :param proxy_target: IPv6 address which is proxied on this interface
               for NDP.
        :returns: None
        :raises: FailedSystemCall
        """
        # VPPSTART
        vpp_inter = if_name.encode('utf-8', 'ignore')
        _log.debug("vppapi: Processing v6 Creation of vpp_inter %s of type %s",
                   vpp_inter, type(vpp_inter))

        # If we already have this interface. Do nothing.
        if if_name in VppDevices.vpp_resolve_if_index:
            _log.debug("vppapi: We already have the calico interace assigned to an if_index: %s name: %s",
                        VppDevices.vpp_resolve_if_index[if_name], if_name)
            return
        _log.debug("VPPDICT before addition: %s", VppDevices.vpp_resolve_if_index)

        afp_r = vpp_papi.af_packet_create(vpp_inter,
                                 "00:00:00:00:00:00", True, False)
        if type(afp_r) != list and afp_r.retval == 0:
            sw_if_index = afp_r.sw_if_index
            admin_up_down = 1
            link_up = 1
            deleted = 0

            # Store the VPP sw_if_index to calico if_name mapping.
            VppDevices.vpp_resolve_if_index[if_name] = sw_if_index

            _log.debug("Associated calico interface %s with vpp if_index %s",
                       if_name, VppDevices.vpp_resolve_if_index[if_name])
            # If create success. Set interface flags to up.
            flags_r = vpp_papi.sw_interface_set_flags(sw_if_index,
                                                      admin_up_down,
                                                      link_up, deleted)

            if type(flags_r) == list or flags_r.retval != 0:
                _log.critical("vppapi: Call to set VPP interface flags"
                              "for %s failed???", vpp_inter)
                return

            _log.debug("vppapi: VPP AFP for %s created", vpp_inter)

        else:
            _log.debug("vppapi: v6_configure: could not create AFP: %s Dict State: %s", vpp_inter, VppDevices.vpp_resolve_if_index)
        # VPPEND
        _write_proc_sys("/proc/sys/net/ipv6/conf/%s/proxy_ndp" % if_name, 1)

        # Allows None if no IPv6 proxy target is required.
        if proxy_target:
            futils.check_call(["ip", "-6", "neigh", "add",
                               "proxy", str(proxy_target), "dev", if_name])

    def _add_route(self, ip_type, ip, interface, mac):
        """
        Add a route to a given interface (including arp config).
        Errors lead to exceptions that are not handled here.

        Note that we use "ip route replace", since that overrides any imported
        routes to the same IP, which might exist in the middle of a migration.

        :param ip_type: Type of IP (IPV4 or IPV6)
        :param str ip: IP address
        :param str interface: Interface name
        :param str mac: MAC address or None to skip programming the ARP cache.
        :raises FailedSystemCall
        """
        # if ip_type == futils.IPV4:
        #     if mac:
        #         futils.check_call(['arp', '-s', ip, mac, '-i', interface])
        #     futils.check_call(["ip", "route", "replace", ip, "dev", interface])
        # else:
        #     futils.check_call(["ip", "-6", "route", "replace", ip, "dev",
        #                        interface])

    def _del_route(self, ip_type, ip, interface):
        """
        Delete a route to a given interface (including arp config).

        :param ip_type: Type of IP (IPV4 or IPV6)
        :param str ip: IP address
        :param str interface: Interface name
        :raises FailedSystemCall
        """
        if ip_type != futils.IPV4:
               _log.debug("vppapi: del_route: got request for IPv6 ... Ignoring for now. ")
               # matjohn2 TODO we dont support V6 yet.
               return

        if len(ips) != 1:
            _log.debug("vppapi: del_route: got too many IPs: %s", ips)
            return

        # VPP AF_PACKET interface adds and deletes now performed in
        # configure_interface_ipv4 and deconfigure_interface_ipv4 respectivley.
        _log.debug("Route canges only will go here for VPP - TODO. NO ACTION TAKEN")

        # if ip_type == futils.IPV4:
        #     futils.check_call(['arp', '-d', ip, '-i', interface])
        #     futils.check_call(["ip", "route", "del", ip, "dev", interface])
        # else:
        #     futils.check_call(["ip", "-6", "route", "del", ip, "dev",
        #                        interface])

    def set_routes(self, ip_type, ips, interface, mac=None, reset_arp=False):
        """
        Set the routes on the interface to be the specified set.

        :param ip_type: Type of IP (IPV4 or IPV6)
        :param set ips: IPs to set up (any not in the set are removed)
        :param str interface: Interface name
        :param str mac|NoneType: MAC address.
        :param bool reset_arp: Reset arp. Only valid if IPv4.
        """
        assert ip_type in (futils.IPV4, futils.IPV6), (
            "Expected an IP type, got %s" % ip_type
        )

        #VPPSTART
        # Interface already created in configure_interface_ipv4 or configure_interface_ipv6.
        # Here we add IP(4/6), ARP/NDP and Routes.
        vpp_inter = interface.encode('utf-8', 'ignore')

        #Worry about one IP for now
        if len(ips) != 1:
            _log.debug("vppapi: set_routes: didnt get a singular IP: %s", ips)
            return

        #Get our VPP if_index back
        if interface not in VppDevices.vpp_resolve_if_index:
            _log.critical("vppapi: Expected an interface mapping in dict (set_routes). Interface: %s Dict: %s",
                        interface, VppDevices.vpp_resolve_if_index)
            return

        str_if_index = VppDevices.vpp_resolve_if_index[interface]
        sw_if_index = int(str_if_index)

        if ip_type == futils.IPV6:
            #IPv6 Configuration
            _log.debug("vppapi: processing request for IPv6 set_routes. ")

            # Calico Tells us the MAC address of the container/workload.
            eui_mac_address = EUI(mac.encode('utf-8', 'ignore'))
            # Deviate the MAC by a couple of bits to use for the VPP end of the link...

            #VPP's LinkLocal IP
            ll_dst_address_str = self.mac2vpplinklocal(str(eui_mac_address))
            ll_dst_address = socket.inet_pton(socket.AF_INET6, ll_dst_address_str)

            ##The CONTAINER's Link-Local IP (in string and binary format)
            ll_container_ip = self.mac2linklocal(str(eui_mac_address))
            ll_container_ip_binary = dst_address = socket.inet_pton(socket.AF_INET6, ll_container_ip)

            #The CONTAINER's MAC address
            eui_mac_address.dialect = mac_bare
            mac_address = str(eui_mac_address)

            #The CONTAINER's /128 route to add (in string format for logging and binary for VPP Calls)
            dst_address_str = ips.pop().encode('utf-8', 'ignore')
            dst_address = socket.inet_pton(socket.AF_INET6, dst_address_str)

            #Vars for All VPP API Calls.
            vpp_vrf_id = 0
            is_add = True
            is_ipv6 = True
            is_static = False

            # Add VPP's Link-Local IPv6 Address
            _log.debug("vppapi: Configuring VPP link-local address %s for ifindex %s", ll_dst_address_str, sw_if_index)

            ll_addr_r = vpp_papi.sw_interface_ip6_set_link_local_address(
                                                sw_if_index,
                                                ll_dst_address, 64)

            if type(ll_addr_r) != list and ll_addr_r.retval == 0:
                _log.debug("vppapi: Configured VPP link-local v6 address: %s to interface %s",
                          ll_dst_address_str, vpp_inter)
            else:
                _log.critical("vppapi: Failed to configure VPP link-local IP %s",
                            ll_dst_address_str)
                #May already be configued. Nasty but for now we'll not return
                #return


            # Setup VPP Neighbor to Containers link-local IP and MAC
            _log.debug("vppapi: Configuring VPP to Container link-local adjacency. container IP: %s ifindex %s", ll_container_ip, sw_if_index)
            nb_ll = vpp_papi.ip_neighbor_add_del(vpp_vrf_id,
                                                 sw_if_index,
                                                 is_add,
                                                 is_ipv6,
                                                 is_static,
                                                 mac_address.decode('hex'),
                                                 ll_container_ip_binary)

            if type(nb_ll) != list and nb_ll.retval == 0:
                 _log.debug("vppapi: Configured VPP to Container link-local adjacency. Container IP: %s MAC: %s IFIndex: %s Interface: %s ",
                       ll_container_ip ,mac_address, sw_if_index, vpp_inter)
            else:
                _log.critical("vppapi: Failed to Configure VPP Container link-local adjacency int: %s mac: %s ip: %s",
                       vpp_inter, mac_address, ll_container_ip)
                #May already be configued. Nasty but for now we'll not return
                #return


            # Setup the requested IP route as a neighbor (The real Calico Assigned IP).
            _log.debug("vppapi: Configuring Requested route %s as a VPP adjacency to interface: %s ifindex: %s",
                    dst_address_str, vpp_inter, sw_if_index)

            nb_r = vpp_papi.ip_neighbor_add_del(vpp_vrf_id,
                                                 sw_if_index,
                                                 is_add,
                                                 is_ipv6,
                                                 is_static,
                                                 mac_address.decode('hex'),
                                                 dst_address)

            if type(nb_r) != list and nb_r.retval == 0:
                 _log.debug("vppapi: Configuring Requested route %s Neighbor / Adjacency with Mac: %s Interface: %s IfIndex: %s",
                      dst_address_str, mac_address, vpp_inter, sw_if_index )
            else:
                _log.critical("vppapi: Failed to Configure Requested route %s Neighbor / Adjacency with Mac: %s Interface: %s IfIndex: %s",
                     dst_address_str, mac_address, vpp_inter, sw_if_index )
                return

            # Setup the requested IP route in VPP (Finally enables Claico IP to work).
            _log.debug("vppapi: Configuring Requested route %s/128 via interface: %s ifindex: %s",
                dst_address_str, vpp_inter, sw_if_index)
            route_r = vpp_papi.ip_add_del_route(sw_if_index,
                                            vpp_vrf_id,
                                            False, 9, 0,
                                            True, True,
                                            is_add, False,
                                            is_ipv6, False,
                                            False, False,
                                            False, False, False, 0,
                                            128, dst_address,
                                            dst_address)

            if type(route_r) != list and route_r.retval == 0:
                _log.debug("vppapi: Configured route %s/128 via interface: %s ifindex: %s",
                    dst_address_str, vpp_inter, sw_if_index)
            else:
                _log.critical("vppapi: Failed to Configure route %s/128 via interface: %s ifindex: %s",
                    dst_address_str, vpp_inter, sw_if_index)
                return


        if ip_type == futils.IPV4:
            #IPv4 Configuration
            _log.debug("vppapi: processing request for IPv4 set_routes. ")
            vpp_vrf_id = 0
            is_add = True
            is_ipv6 = False
            is_static = False

            eui_mac_address = EUI(mac.encode('utf-8', 'ignore'))
            eui_mac_address.dialect = mac_bare
            mac_address = str(eui_mac_address)
            dst_address_str = ips.pop().encode('utf-8', 'ignore')
            dst_address = socket.inet_pton(socket.AF_INET, dst_address_str)

            nb_r = vpp_papi.ip_neighbor_add_del(vpp_vrf_id,
                                                sw_if_index,
                                                is_add,
                                                is_ipv6,
                                                is_static,
                                                mac_address.decode('hex'),
                                                dst_address)

            if type(nb_r) != list and nb_r.retval == 0:
                _log.debug("vppapi: VPP AFP %s added static arp"
                         " %s for %s",
                          vpp_inter, mac_address, dst_address_str)
            else:
                _log.critical("vppapi: VPP AFP add arp failed on int %s mac %s addr %s",
                          vpp_inter, mac_address, dst_address_str)
                return

            route_r = vpp_papi.ip_add_del_route(sw_if_index,
                                                vpp_vrf_id,
                                                False, 9, 0,
                                                True, True,
                                                is_add, False,
                                                is_ipv6, False,
                                                False, False,
                                                False, False, False, 0,
                                                32, dst_address,
                                                dst_address)
            if type(route_r) != list and route_r.retval == 0:
                _log.debug("vppapi: added static route for %s",
                          dst_address_str)
            else:
                _log.critical("vppapi: Could not add route %s", dst_address_str)
                return

            proxyarp_r = vpp_papi.proxy_arp_intfc_enable_disable(
                sw_if_index, True)
            if type(proxyarp_r) != list and proxyarp_r.retval == 0:
                _log.debug("vppapi: enabled proxy arp for sw_if_index %s",
                        sw_if_index)
            else:
                _log.critical("vppapi: Could not enable proxy arp for"
                              " sw_if_index:  %s", sw_if_index)
                return

            #VPPEND
            # if reset_arp and ip_type != futils.IPV4:
            #     raise ValueError("reset_arp may only be supplied for IPv4")
            #
            # current_ips = self.list_interface_route_ips(ip_type, interface)
            #
            # removed_ips = (current_ips - ips)
            # for ip in removed_ips:
            #     self._del_route(ip_type, ip, interface)
            # for ip in (ips - current_ips):
            #     self._add_route(ip_type, ip, interface, mac)
            # if mac and reset_arp:
            #     for ip in (ips & current_ips):
            #         futils.check_call(['arp', '-s', ip, mac, '-i', interface])

    def interface_up(self, if_name):
        """
        Checks whether a given interface is up.

        Check this by examining the operstate of the interface, which is the
        highest level "is it ready to work with" flag.

        :param str if_name: Interface name
        :returns: True if interface up, False if down or cannot detect
        """
        operstate_filename = '/sys/class/net/%s/operstate' % if_name
        try:
            with open(operstate_filename, 'r') as f:
                oper_state = f.read().strip()
        except IOError as e:
            # If we fail to check that the interface is up, then it has
            # probably gone under our feet or is flapping.
            _log.warning("Failed to read state of interface %s (%s) - assume "
                         "down/absent: %r.", if_name, operstate_filename, e)
            return False
        else:
            _log.debug("Interface %s has state %s", if_name, oper_state)
        return oper_state == "up"

    def deconfigure_interface_ipv4(self, if_name):
        vpp_inter = if_name.encode('utf-8', 'ignore')
        _log.debug("vppapi: Processing removal of vpp_inter %s of type %s",
                    vpp_inter, type(vpp_inter))

        afp_r = vpp_papi.af_packet_delete(vpp_inter)

        if type(afp_r) != list and afp_r.retval == 0:
            _log.debug("vppapi: Sucessfully removed interface %s of type %s",
                       vpp_inter, type(vpp_inter))
        else:
            _log.critical("vppapi: Could not remove interface %s of type %s",
                         vpp_inter, type(vpp_inter))
            return

    def remove_conntrack_flows(self, ip_addresses, ip_version):
        """
        Removes any conntrack entries that use any of the given IP
        addresses in their source/destination.
        """
        assert ip_version in (4, 6)
        for ip in ip_addresses:
            _log.debug("Removing conntrack rules for %s", ip)
            for direction in ["--orig-src", "--orig-dst",
                              "--reply-src", "--reply-dst"]:
                try:
                    futils.check_call(["conntrack", "--family",
                                       "ipv%s" % ip_version, "--delete",
                                       direction, ip])
                except FailedSystemCall as e:
                    if e.retcode == 1 and "0 flow entries" in e.stderr:
                        # Expected if there are no flows.
                        _log.debug("No conntrack entries found for %s/%s.",
                                   ip, direction)
                    else:
                        # Suppress the exception, conntrack entries will
                        # timeout and it's hard to think of an example where
                        # killing and restarting felix would help.
                        _log.exception(
                            "Failed to remove conntrack flows for %s. "
                            "Ignoring.", ip
                        )

    def interface_watcher(self, update_splitter):
        return InterfaceWatcher(update_splitter)


def _read_proc_sys(name):
    with open(name, "rb") as f:
        return f.read().strip()


def _write_proc_sys(name, value):
    with open(name, "wb") as f:
        f.write(str(value))


# These constants map to constants in the Linux kernel. This is a bit poor, but
# the kernel can never change them, so live with it for now.
RTMGRP_LINK = 1

NLMSG_NOOP = 1
NLMSG_ERROR = 2

RTM_NEWLINK = 16
RTM_DELLINK = 17

IFLA_IFNAME = 3
IFLA_OPERSTATE = 16
IF_OPER_UP = 6


class RTNetlinkError(Exception):
    """
    How we report an error message.
    """
    pass


class InterfaceWatcher(Actor):
    def __init__(self, update_splitter):
        super(InterfaceWatcher, self).__init__()
        self.update_splitter = update_splitter
        self.interfaces = {}

    @actor_message()
    def watch_interfaces(self):
        """
        Detects when interfaces appear, sending notifications to the update
        splitter.

        :returns: Never returns.
        """
        # Create the netlink socket and bind to RTMGRP_LINK,
        s = socket.socket(socket.AF_NETLINK,
                          socket.SOCK_RAW,
                          socket.NETLINK_ROUTE)
        s.bind((os.getpid(), RTMGRP_LINK))

        # A dict that remembers the detailed flags of an interface
        # when we last signalled it as being up.  We use this to avoid
        # sending duplicate interface_update signals.
        if_last_flags = {}

        while True:
            # Get the next set of data.
            data = s.recv(65535)

            # First 16 bytes is the message header; unpack it.
            hdr = data[:16]
            data = data[16:]
            msg_len, msg_type, flags, seq, pid = struct.unpack("=LHHLL", hdr)

            if msg_type == NLMSG_NOOP:
                # Noop - get some more data.
                continue
            elif msg_type == NLMSG_ERROR:
                # We have got an error. Raise an exception which brings the
                # process down.
                raise RTNetlinkError("Netlink error message, header : %s",
                                     futils.hex(hdr))
            _log.debug("Netlink message type %s len %s", msg_type, msg_len)

            if msg_type in [RTM_NEWLINK, RTM_DELLINK]:
                # A new or removed interface.  Read the struct
                # ifinfomsg, which is 16 bytes.
                hdr = data[:16]
                data = data[16:]
                _, _, _, index, flags, _ = struct.unpack("=BBHiII", hdr)
                _log.debug("Interface index %s flags %x", index, flags)

                # Bytes left is the message length minus the two headers of 16
                # bytes each.
                remaining = msg_len - 32

                # Loop through attributes, looking for the pieces of
                # information that we need.
                ifname = None
                operstate = None
                while remaining:
                    # The data content is an array of RTA objects, each of
                    # which has a 4 byte header and some data.
                    rta_len, rta_type = struct.unpack("=HH", data[:4])

                    # This check comes from RTA_OK, and terminates a string of
                    # routing attributes.
                    if rta_len < 4:
                        break

                    rta_data = data[4:rta_len]

                    # Remove the RTA object from the data. The length to jump
                    # is the rta_len rounded up to the nearest 4 byte boundary.
                    increment = int((rta_len + 3) / 4) * 4
                    data = data[increment:]
                    remaining -= increment

                    if rta_type == IFLA_IFNAME:
                        ifname = rta_data[:-1]
                        _log.debug("IFLA_IFNAME: %s", ifname)
                    elif rta_type == IFLA_OPERSTATE:
                        operstate, = struct.unpack("=B", rta_data[:1])
                        _log.debug("IFLA_OPERSTATE: %s", operstate)

                if (ifname and
                        (msg_type == RTM_DELLINK or operstate != IF_OPER_UP)):
                    # The interface is down; make sure the other actors know
                    # about it.
                    self.update_splitter.on_interface_update(ifname,
                                                             iface_up=False)
                    # Remove any record we had of the interface so that, when
                    # it goes back up, we'll report that.
                    if_last_flags.pop(ifname, None)

                if (ifname and
                    msg_type == RTM_NEWLINK and
                    operstate == IF_OPER_UP and
                    (ifname not in if_last_flags or
                     if_last_flags[ifname] != flags)):
                    # We only care about notifying when a new
                    # interface is usable, which - according to
                    # https://www.kernel.org/doc/Documentation/networking/
                    # operstates.txt - is fully conveyed by the
                    # operstate.  (When an interface goes away, it
                    # automatically takes its routes with it.)
                    _log.debug("New network interface : %s %x", ifname, flags)
                    if_last_flags[ifname] = flags
                    self.update_splitter.on_interface_update(ifname,
                                                             iface_up=True)


class BadKernelConfig(Exception):
    pass
