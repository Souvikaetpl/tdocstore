"""A minimal OAuth Authorization Server, implementing the SDK's
OAuthAuthorizationServerProvider protocol, for MCP clients that only
accept a server URL (Claude.ai/ChatGPT-style connectors) with nowhere
to paste a personal token. It reuses the same login this project
already has (Google or a local test account) as the actual identity
check, and the same mcp_tokens table as the actual access token — the
only new thing here is the standard redirect dance that lets a client
obtain one of those tokens automatically instead of a person copying
and pasting it.
"""
from typing import Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from crawler import db as crawler_db
from . import queries

# Where our own consent page lives — the website's own origin, not the
# MCP server's (port 8001). authorize() just returns a redirect URL; it
# can point anywhere, same as redirecting out to a third-party login.
_CONSENT_URL_BASE = "http://localhost:8000/oauth/consent"


class LocalOAuthProvider(OAuthAuthorizationServerProvider):
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        with crawler_db.session() as conn:
            row = queries.get_oauth_client(conn, client_id)
        if row is None:
            return None
        return OAuthClientInformationFull(
            client_id=row["client_id"], client_secret=row["client_secret"],
            redirect_uris=row["redirect_uris"] or None, client_name=row["client_name"],
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.redirect_uris:
            raise RegistrationError(error="invalid_redirect_uri", error_description="redirect_uris is required")
        with crawler_db.session() as conn:
            queries.register_oauth_client(
                conn, client_id=client_info.client_id, client_secret=client_info.client_secret,
                redirect_uris=[str(u) for u in client_info.redirect_uris], client_name=client_info.client_name,
                token_endpoint_auth_method=client_info.token_endpoint_auth_method,
            )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        with crawler_db.session() as conn:
            request_id = queries.create_oauth_authorize_request(
                conn, client_id=client.client_id, redirect_uri=str(params.redirect_uri),
                code_challenge=params.code_challenge, state=params.state, scopes=params.scopes or [],
            )
        return f"{_CONSENT_URL_BASE}?request_id={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        with crawler_db.session() as conn:
            row = queries.get_authorization_code(conn, authorization_code)
        if row is None or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=row["scopes"], expires_at=row["expires_at"],
            client_id=row["client_id"], code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"], redirect_uri_provided_explicitly=True,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with crawler_db.session() as conn:
            row = queries.get_authorization_code(conn, authorization_code.code)
            if row is None:
                raise TokenError(error="invalid_grant", error_description="code expired, already used, or unknown")
            queries.consume_authorization_code(conn, authorization_code.code)
            try:
                token = queries.create_mcp_token(
                    conn, row["user_id"], name=f"OAuth: {client.client_name or client.client_id}"
                )
            except (queries.McpAdminOnlyError, queries.McpTokenLimitExceeded) as exc:
                raise TokenError(error="access_denied", error_description=str(exc))
        if token is None:
            # The consent page already checks this and shouldn't have let
            # the user approve in the first place — this is the same
            # check enforced again at the one place a token actually gets
            # minted, so there's no path (UI bug, a stale approved page,
            # a client that skips the consent redirect somehow) that
            # bypasses it.
            raise TokenError(error="access_denied", error_description="MCP access has been disabled for this account")
        # No refresh_token: these access tokens don't expire on their own
        # (same as a self-service personal token) — only an explicit
        # revoke ends one, so there's nothing for a refresh grant to do.
        return OAuthToken(access_token=token, token_type="Bearer", scope=" ".join(authorization_code.scopes))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        raise TokenError(error="unsupported_grant_type", error_description="refresh tokens are not issued")

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        with crawler_db.session() as conn:
            user = queries.get_user_by_mcp_token(conn, token)
        if user is None:
            return None
        return AccessToken(token=token, client_id="", scopes=["mcp"], subject=str(user.id))

    async def revoke_token(self, token) -> None:
        with crawler_db.session() as conn:
            queries.revoke_mcp_token_by_raw(conn, token.token)
