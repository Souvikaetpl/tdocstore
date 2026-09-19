from authlib.integrations.starlette_client import OAuth

from . import config

oauth = OAuth()
oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    client_kwargs={"scope": "openid email profile"},
)
