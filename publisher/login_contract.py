ISSUER = "https://auth.iflink.tech/oidc"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 48321
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
LOGIN_PATH = "/v1/cli/login"
DEFAULT_URL = "https://deploy.w33d.xyz"
PRODUCTION_SCOPES = ['projects:read', 'deployments:read', 'deployments:write']
