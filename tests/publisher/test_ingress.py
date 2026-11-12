import datetime
import http.server
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from publisher.backend import BackendError, Dokploy, OriginHTTPSConnection
from publisher.config import Settings


class OriginIngressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deployment = {'id': 'a' * 24, 'project_id': 'b' * 24, 'app_domain': 'example.test'}
        self.config = {'releases': str(self.root / 'releases'), 'ingress_probe': 'origin',
                       'origin_probe_address': '127.0.0.1', 'origin_probe_ca': str(self.root / 'ca.pem')}
        self.backend = Dokploy(self.config, Settings(self.root))

    def test_waits_for_origin_binding_instead_of_accepting_an_unrouted_404(self):
        connection = Mock()
        missing, wrong, ready = Mock(status=404), Mock(status=204), Mock(status=204)
        missing.getheader.return_value = None
        wrong.getheader.return_value = 'other-deployment'
        ready.getheader.return_value = self.deployment['id']
        connection.getresponse.side_effect = [missing, wrong, ready]
        with patch('publisher.backend.ssl.create_default_context') as context, \
             patch('publisher.backend.OriginHTTPSConnection', return_value=connection) as connect, \
             patch('publisher.backend.time.sleep'):
            self.backend.public_health(self.deployment)
        context.assert_called_once_with(cafile=self.config['origin_probe_ca'])
        self.assertEqual(3, connection.request.call_count)
        connect.assert_called_with(self.backend.hostname(self.deployment), '127.0.0.1', context.return_value)
        self.assertEqual('/__paas_route_probe__', connection.request.call_args.args[1])
        self.assertEqual(3, connection.close.call_count)

    def test_missing_origin_route_expires_without_becoming_ready(self):
        connection = Mock()
        connection.getresponse.return_value.status = 404
        connection.getresponse.return_value.getheader.return_value = None
        with patch('publisher.backend.ssl.create_default_context'), \
             patch('publisher.backend.OriginHTTPSConnection', return_value=connection), \
             patch('publisher.backend.time.monotonic', side_effect=[0, 0, 91]), \
             patch('publisher.backend.time.sleep'), \
             self.assertRaisesRegex(BackendError, 'Origin HTTPS endpoint is not ready'):
            self.backend.public_health(self.deployment)
        connection.close.assert_called_once()

    def test_origin_requires_an_ip_and_a_readable_absolute_ca_file(self):
        for extra in ({'origin_probe_address': 'https://origin.test'},
                      {'origin_probe_ca': 'relative.pem'}, {'origin_probe_ca': str(self.root / 'missing.pem')}):
            with self.subTest(extra=extra):
                backend = Dokploy(self.config | extra, Settings(self.root))
                with self.assertRaisesRegex(BackendError, 'Origin ingress requires'):
                    backend.public_health(self.deployment)

    def start_tls_origin(self):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'origin.test')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('origin.test')]), critical=False)
                .sign(key, hashes.SHA256()))
        ca, private = self.root / 'ca.pem', self.root / 'key.pem'
        ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(('request', self.headers['Host']))
                self.send_response(204)
                self.send_header('X-Paas-Deployment', 'test-release')
                self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ca, private)
        context.set_servername_callback(lambda sock, name, ctx: seen.append(('sni', name)))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server.server_address[1], seen

    def test_fixed_address_preserves_verified_hostname_sni_and_host(self):
        port, seen = self.start_tls_origin()
        context = ssl.create_default_context(cafile=self.config['origin_probe_ca'])
        connection = OriginHTTPSConnection('origin.test', '127.0.0.1', context, port=port)
        self.addCleanup(connection.close)
        connection.request('GET', '/__paas_route_probe__')
        response = connection.getresponse()
        self.assertEqual(204, response.status)
        self.assertEqual('test-release', response.getheader('X-Paas-Deployment'))
        self.assertIn(('sni', 'origin.test'), seen)
        self.assertIn(('request', 'origin.test:' + str(port)), seen)

    def test_wrong_certificate_hostname_is_rejected(self):
        port, _ = self.start_tls_origin()
        context = ssl.create_default_context(cafile=self.config['origin_probe_ca'])
        connection = OriginHTTPSConnection('other.test', '127.0.0.1', context, port=port)
        self.addCleanup(connection.close)
        with self.assertRaises(ssl.SSLCertVerificationError):
            connection.request('GET', '/')

    def test_untrusted_ca_is_rejected(self):
        port, _ = self.start_tls_origin()
        connection = OriginHTTPSConnection('origin.test', '127.0.0.1', ssl.create_default_context(), port=port)
        self.addCleanup(connection.close)
        with self.assertRaises(ssl.SSLCertVerificationError):
            connection.request('GET', '/')
