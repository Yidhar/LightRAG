from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from ..utils import logger
from .auth_accounts import parse_auth_accounts
from .config import DEFAULT_TOKEN_SECRET, global_args
from .passwords import verify_password

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)


class MembershipClaim(BaseModel):
    workspace_id: str
    kb_id: str | None = None
    role: str


class TokenPayload(BaseModel):
    sub: str  # Username
    exp: datetime  # Expiration time
    username: str | None = None
    uid: str | None = None
    role: str = "user"  # User role, default is regular user
    memberships: list[MembershipClaim] = Field(default_factory=list)
    jti: str = ""
    metadata: dict = Field(default_factory=dict)  # Additional metadata


class AuthHandler:
    def __init__(self):
        auth_accounts = global_args.auth_accounts
        use_db_auth = bool(getattr(global_args, "use_db_auth", False))
        self.secret = global_args.token_secret
        if not self.secret:
            if auth_accounts or use_db_auth:
                raise ValueError(
                    "TOKEN_SECRET must be explicitly set to a non-default value when local authentication is configured."
                )
            self.secret = DEFAULT_TOKEN_SECRET
            logger.warning(
                "TOKEN_SECRET not set and no local auth provider is configured. "
                "Falling back to the default development JWT secret."
            )
        algorithm = global_args.jwt_algorithm
        if not algorithm or algorithm.lower() == "none":
            raise ValueError(
                "JWT_ALGORITHM must be set to a secure algorithm (e.g. HS256). "
                "The 'none' algorithm is not permitted."
            )
        self.algorithm = algorithm
        self.expire_hours = global_args.token_expire_hours
        self.guest_expire_hours = global_args.guest_token_expire_hours
        self.refresh_expire_hours = getattr(global_args, "refresh_token_expire_hours", 168)
        try:
            self.accounts = parse_auth_accounts(auth_accounts)
        except ValueError as exc:
            logger.error(str(exc))
            raise

    def verify_password(self, username: str, plain_password: str) -> bool:
        """
        Verify password for a user. Supports explicit bcrypt values and plaintext.

        Args:
            username: Username to verify
            plain_password: Plaintext password to check

        Returns:
            bool: True if password is correct, False otherwise
        """
        if username not in self.accounts:
            return False

        stored_password = self.accounts[username]
        return verify_password(plain_password, stored_password)

    def create_token(
        self,
        username: str,
        role: str = "user",
        custom_expire_hours: int = None,
        metadata: dict = None,
        memberships: Sequence[MembershipClaim | dict] | None = None,
        jti: str = "",
        user_id: str | None = None,
    ) -> str:
        """
        Create JWT token

        Args:
            username: Username
            role: User role, default is "user", guest is "guest"
            custom_expire_hours: Custom expiration time (hours), if None use default value
            metadata: Additional metadata

        Returns:
            str: Encoded JWT token
        """
        # Choose default expiration time based on role
        if custom_expire_hours is None:
            if role == "guest":
                expire_hours = self.guest_expire_hours
            else:
                expire_hours = self.expire_hours
        else:
            expire_hours = custom_expire_hours

        expire = datetime.now(timezone.utc) + timedelta(hours=expire_hours)

        membership_claims = [
            claim
            if isinstance(claim, MembershipClaim)
            else MembershipClaim.model_validate(claim)
            for claim in (memberships or [])
        ]

        # Create payload
        payload = TokenPayload(
            sub=username,
            exp=expire,
            username=username,
            uid=user_id,
            role=role,
            memberships=membership_claims,
            jti=jti or "",
            metadata=metadata or {},
        )

        return jwt.encode(payload.model_dump(), self.secret, algorithm=self.algorithm)

    def validate_token(self, token: str) -> dict:
        """
        Validate JWT token

        Args:
            token: JWT token

        Returns:
            dict: Dictionary containing user information

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            # Explicitly exclude 'none' to prevent algorithm confusion attacks
            allowed_algorithms = [self.algorithm]
            if "none" in (a.lower() for a in allowed_algorithms):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Insecure JWT algorithm configuration",
                )
            payload = jwt.decode(token, self.secret, algorithms=allowed_algorithms)
            expire_timestamp = payload["exp"]
            expire_time = datetime.fromtimestamp(expire_timestamp, timezone.utc)

            if datetime.now(timezone.utc) > expire_time:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
                )

            memberships = payload.get("memberships") or []
            validated_memberships = [
                MembershipClaim.model_validate(claim).model_dump()
                for claim in memberships
            ]

            username = payload.get("username") or payload["sub"]

            # Return complete payload instead of just username
            return {
                "username": username,
                "user_id": payload.get("uid"),
                "role": payload.get("role", "user"),
                "memberships": validated_memberships,
                "jti": payload.get("jti", ""),
                "metadata": payload.get("metadata", {}),
                "exp": expire_time,
            }
        except (jwt.PyJWTError, KeyError, TypeError, ValidationError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
            )


auth_handler = AuthHandler()
