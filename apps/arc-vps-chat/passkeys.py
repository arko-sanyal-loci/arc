"""WebAuthn ceremonies for Arc. Endpoint authorization is the caller's job."""
import json

from webauthn import (generate_authentication_options, generate_registration_options,
                      options_to_json, verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)


class Passkeys:
    def __init__(self, challenges, rp_id, origin):
        self.challenges = challenges
        self.rp_id = rp_id
        self.origin = origin

    def registration_options(self, username, user_id, excluded=()):
        identifier, challenge = self.challenges.issue('register')
        options = generate_registration_options(
            rp_id=self.rp_id, rp_name='Arc', user_name=username, user_id=user_id,
            challenge=challenge,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=item) for item in excluded])
        return identifier, json.loads(options_to_json(options))

    def verify_registration(self, identifier, credential):
        challenge = self.challenges.consume(identifier, 'register')
        if challenge is None:
            raise ValueError('challenge expired or already used')
        return verify_registration_response(credential=credential, expected_challenge=challenge,
                                            expected_rp_id=self.rp_id, expected_origin=self.origin,
                                            require_user_verification=True)

    def authentication_options(self, credential_ids):
        if not credential_ids:
            raise ValueError('no passkeys enrolled')
        identifier, challenge = self.challenges.issue('authenticate')
        options = generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge,
            user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[PublicKeyCredentialDescriptor(id=item) for item in credential_ids])
        return identifier, json.loads(options_to_json(options))

    def verify_authentication(self, identifier, credential, public_key, sign_count):
        challenge = self.challenges.consume(identifier, 'authenticate')
        if challenge is None:
            raise ValueError('challenge expired or already used')
        return verify_authentication_response(credential=credential, expected_challenge=challenge,
            expected_rp_id=self.rp_id, expected_origin=self.origin, credential_public_key=public_key,
            credential_current_sign_count=sign_count, require_user_verification=True)
