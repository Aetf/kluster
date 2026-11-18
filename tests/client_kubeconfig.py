"""A kubeconfig in the shape the Talos provider issues the cluster-admin one: a client certificate under the current context's user.

A helper module of its own, for the reason `mock_monitor` is one: every suite
that runs the `k8s-base` program configures it with a kubeconfig, the program
reads the expiry of that kubeconfig's client certificate
(`kluster.lib.k8s.client_certificate_expiry`), and test modules do not import
each other.

The certificate is generated, self-signed, with the `NotAfter` its caller
asks for. The cluster it names is reached by nothing.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from collections.abc import Mapping

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

__all__ = ('KUBECONFIG', 'NOT_AFTER', 'USER', 'certificate_user', 'kubeconfig')

#: The user and the context the kubeconfig names, as Talos names them for a
#: cluster called `kluster`.
USER = 'admin@kluster'

#: When `KUBECONFIG`'s client certificate expires: a second boundary, as a
#: certificate's `NotAfter` is.
NOT_AFTER = dt.datetime(2027, 11, 17, 6, 30, 15, tzinfo=dt.UTC)


def certificate_user(not_after: dt.datetime) -> dict[str, str]:
    """A kubeconfig user that authenticates with a generated client certificate expiring at `not_after`."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'system:masters'),
            x509.NameAttribute(NameOID.COMMON_NAME, 'admin'),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_after - dt.timedelta(days=365))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return {
        'client-certificate-data': base64.b64encode(certificate.public_bytes(serialization.Encoding.PEM)).decode(),
        'client-key-data': base64.b64encode(key_pem).decode(),
    }


def kubeconfig(user: Mapping[str, str]) -> str:
    """A kubeconfig whose current context authenticates as `user`, written as JSON, which is YAML."""
    return json.dumps(
        {
            'apiVersion': 'v1',
            'kind': 'Config',
            'clusters': [{'name': 'kluster', 'cluster': {'server': 'https://a-cluster-nothing-reaches.invalid:6443'}}],
            'users': [{'name': USER, 'user': dict(user)}],
            'contexts': [{'name': USER, 'context': {'cluster': 'kluster', 'namespace': 'default', 'user': USER}}],
            'current-context': USER,
        }
    )


#: The kubeconfig a suite configures the `k8s-base` program with.
KUBECONFIG = kubeconfig(certificate_user(NOT_AFTER))
