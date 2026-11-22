"""Exercise real WebAuthn signature validation with an ephemeral software key."""
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).parents[1]))
from challenge_store import ChallengeStore
from credential_store import CredentialStore
from passkeys import Passkeys


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def client(kind, challenge, origin='https://billo.tech'):
    return json.dumps({'type': kind, 'challenge': challenge, 'origin': origin}, separators=(',', ':')).encode()


class PasskeyCeremonyTests(unittest.TestCase):
    def test_register_authenticate_and_reject_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            api = Passkeys(ChallengeStore(path / 'challenges'), 'billo.tech', 'https://billo.tech')
            store = CredentialStore(path / 'credentials')
            key = ec.generate_private_key(ec.SECP256R1())
            numbers = key.public_key().public_numbers()
            cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})
            credential_id = b'test-credential-id'
            rp_hash = hashlib.sha256(b'billo.tech').digest()
            challenge_id, options = api.registration_options('owner', b'owner-id')
            client_data = client('webauthn.create', options['challenge'])
            auth_data = rp_hash + bytes([0x45]) + struct.pack('>I', 0) + bytes(16) + struct.pack('>H', len(credential_id)) + credential_id + cose
            attestation = cbor2.dumps({'fmt': 'none', 'authData': auth_data, 'attStmt': {}})
            registration = {'id': b64(credential_id), 'rawId': b64(credential_id), 'type': 'public-key', 'response': {'clientDataJSON': b64(client_data), 'attestationObject': b64(attestation)}}
            verified = api.verify_registration(challenge_id, registration)
            store.add(verified.credential_id, verified.credential_public_key, verified.sign_count)
            challenge_id, options = api.authentication_options(store.ids())
            client_data = client('webauthn.get', options['challenge'])
            auth_data = rp_hash + bytes([0x05]) + struct.pack('>I', 1)
            signature = key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
            assertion = {'id': b64(credential_id), 'rawId': b64(credential_id), 'type': 'public-key', 'response': {'clientDataJSON': b64(client_data), 'authenticatorData': b64(auth_data), 'signature': b64(signature)}}
            public_key, counter = store.get(credential_id)
            result = api.verify_authentication(challenge_id, assertion, public_key, counter)
            self.assertTrue(store.update_counter(credential_id, counter, result.new_sign_count))
            with self.assertRaises(ValueError):
                api.verify_authentication(challenge_id, assertion, public_key, counter)
            challenge_id, options = api.authentication_options(store.ids())
            bad_client = client('webauthn.get', options['challenge'], 'https://evil.example')
            assertion['response']['clientDataJSON'] = b64(bad_client)
            with self.assertRaises(Exception):
                api.verify_authentication(challenge_id, assertion, public_key, 1)
