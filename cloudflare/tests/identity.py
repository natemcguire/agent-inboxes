"""Fresh signing keys for tests; never used by the deployed Access verifier."""
import base64
import json
import time
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


class Identity:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwks = {'keys':[{'kty':'RSA','kid':'integration','alg':'RS256','use':'sig',
            'n':b64(numbers.n.to_bytes(256,'big')),'e':b64(numbers.e.to_bytes(3,'big'))}]}

    def token(self, email='nate@example.com', **overrides):
        header = b64(json.dumps({'alg':'RS256','kid':'integration'}).encode())
        claims = {'iss':'https://integration.cloudflareaccess.com','aud':['integration-audience'],
            'email':email,'exp':int(time.time())+3600,'nbf':int(time.time())-1}
        claims.update(overrides)
        payload = b64(json.dumps(claims).encode())
        data = (header+'.'+payload).encode()
        return header+'.'+payload+'.'+b64(self.key.sign(data,padding.PKCS1v15(),hashes.SHA256()))
