"""Explicit HTTPS egress policy with DNS validation and pinned socket connections."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit


class EgressPolicy:
    def __init__(self, hosts=()):
        self.hosts = frozenset(h.strip().lower() for h in hosts if h.strip())
        for host in self.hosts:
            if not host or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host):
                raise ValueError("Egress hosts must be exact ASCII hostnames, not URLs or wildcards.")

    def validate(self, url):
        if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 or ord(c) > 126 for c in url) or "\\" in url:
            raise ValueError("Invalid HTTPS target.")
        p = urlsplit(url)
        if (p.scheme != "https" or p.hostname not in self.hosts or p.port not in (None, 443)
                or p.username is not None or p.password is not None or p.query or p.fragment):
            raise ValueError("Use an explicitly allowlisted HTTPS hostname on port 443, without credentials, query or fragment.")
        return p

    def resolve(self, host):
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not answers:
            raise ValueError("Target has no addresses.")
        for family, _, _, _, address in answers:
            ip = ipaddress.ip_address(address[0])
            if (family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global
                    or ip.is_multicast or ip.is_unspecified
                    or (isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))):
                raise ValueError("Target resolves to a prohibited address.")
        return answers[0]

    def connection(self, url):
        p = self.validate(url)
        answer = self.resolve(p.hostname)
        return PinnedHTTPSConnection(p.hostname, answer), p.path or "/"


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, answer):
        super().__init__(host, 443, timeout=3, context=ssl.create_default_context())
        self.answer = answer

    def connect(self):
        family, socktype, proto, _, address = self.answer
        raw = socket.socket(family, socktype, proto)
        try:
            raw.settimeout(self.timeout)
            raw.connect(address)
            # Keep hostname verification and SNI while connecting to the validated IP.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
