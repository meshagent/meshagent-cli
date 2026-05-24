import json
import os
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel, Field

from meshagent.api.client import User

DEFAULT_API_URL = "https://api.meshagent.com"
PROFILE_API_URL_ENV = "MESHAGENT_PROFILE_API_URL"
SETTINGS_DIR = Path.home() / ".meshagent"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
LEGACY_SESSION_FILE = SETTINGS_DIR / "session.json"
LEGACY_PROJECT_FILE = SETTINGS_DIR / "project.json"
LOCAL_STATE_USER_ID = "__local__"


class StoredSession(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    token_type: str = "Bearer"
    scope: str | None = None
    id_token: str | None = None

    def is_empty(self) -> bool:
        return (
            self.access_token is None
            and self.refresh_token is None
            and self.expires_at is None
            and self.scope is None
            and self.id_token is None
            and self.token_type == "Bearer"
        )


class StoredProjectSettings(BaseModel):
    active_project: str | None = None
    active_api_keys: dict[str, str] = Field(default_factory=dict)
    llm_proxy_bearer_token: str | None = None

    def is_empty(self) -> bool:
        return (
            self.active_project is None
            and len(self.active_api_keys) == 0
            and self.llm_proxy_bearer_token is None
        )


class StoredUserProfile(BaseModel):
    id: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None

    @classmethod
    def from_user(cls, user: User) -> "StoredUserProfile":
        return cls(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
        )

    def display_name(self) -> str:
        parts = []
        if isinstance(self.first_name, str):
            first_name = self.first_name.strip()
            if first_name != "":
                parts.append(first_name)
        if isinstance(self.last_name, str):
            last_name = self.last_name.strip()
            if last_name != "":
                parts.append(last_name)

        if len(parts) > 0:
            return " ".join(parts)

        if isinstance(self.email, str):
            email = self.email.strip()
            if email != "":
                return email

        return self.id


class StoredUserSettings(BaseModel):
    profile: StoredUserProfile | None = None
    api_url: str | None = None
    session: StoredSession | None = None
    project: StoredProjectSettings = Field(default_factory=StoredProjectSettings)

    def is_empty(self) -> bool:
        return (
            self.profile is None
            and self.api_url is None
            and (self.session is None or self.session.is_empty())
            and self.project.is_empty()
        )


class CLISettings(BaseModel):
    active_user_id: str | None = None
    users: dict[str, StoredUserSettings] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SavedProfileRecord:
    user_id: str
    profile: StoredUserProfile
    api_url: str | None
    is_active: bool


def normalize_api_url(api_url: str | None) -> str | None:
    if api_url is None:
        return None

    normalized = api_url.strip()
    if normalized == "":
        return None

    return normalized.rstrip("/")


def _ensure_settings_dir() -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_settings(settings: CLISettings) -> None:
    local_state = settings.users.get(LOCAL_STATE_USER_ID)
    if local_state is not None and local_state.is_empty():
        del settings.users[LOCAL_STATE_USER_ID]
        if settings.active_user_id == LOCAL_STATE_USER_ID:
            settings.active_user_id = None

    if (
        settings.active_user_id is not None
        and settings.active_user_id not in settings.users
    ):
        settings.active_user_id = None


def _save_settings(settings: CLISettings) -> None:
    _cleanup_settings(settings)
    _ensure_settings_dir()
    SETTINGS_FILE.write_text(
        json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True)
    )


def _load_json_model(path: Path, model_type: type[BaseModel]) -> BaseModel | None:
    if not path.exists():
        return None
    return model_type.model_validate_json(path.read_text())


def _migrate_legacy_settings() -> CLISettings | None:
    legacy_session = _load_json_model(LEGACY_SESSION_FILE, StoredSession)
    legacy_project = _load_json_model(LEGACY_PROJECT_FILE, StoredProjectSettings)
    if legacy_session is None and legacy_project is None:
        return None

    local_settings = StoredUserSettings(
        api_url=normalize_api_url(os.getenv("MESHAGENT_API_URL")),
        session=legacy_session if isinstance(legacy_session, StoredSession) else None,
        project=(
            legacy_project
            if isinstance(legacy_project, StoredProjectSettings)
            else StoredProjectSettings()
        ),
    )
    return CLISettings(
        active_user_id=LOCAL_STATE_USER_ID,
        users={LOCAL_STATE_USER_ID: local_settings},
    )


def _remove_legacy_files() -> None:
    LEGACY_SESSION_FILE.unlink(missing_ok=True)
    LEGACY_PROJECT_FILE.unlink(missing_ok=True)


def load_settings() -> CLISettings:
    try:
        _ensure_settings_dir()
        if SETTINGS_FILE.exists():
            return CLISettings.model_validate_json(SETTINGS_FILE.read_text())

        migrated = _migrate_legacy_settings()
        if migrated is None:
            return CLISettings()

        try:
            _save_settings(migrated)
            _remove_legacy_files()
        except OSError as ex:
            if ex.errno not in (1, 30):
                raise
        return migrated
    except OSError as ex:
        if ex.errno in (1, 30):
            return CLISettings()
        raise


def _active_user_id(settings: CLISettings) -> str | None:
    if (
        settings.active_user_id is not None
        and settings.active_user_id in settings.users
    ):
        return settings.active_user_id

    if LOCAL_STATE_USER_ID in settings.users:
        return LOCAL_STATE_USER_ID

    if len(settings.users) == 1:
        return next(iter(settings.users))

    return None


def _active_user(settings: CLISettings) -> StoredUserSettings | None:
    user_id = _active_user_id(settings)
    if user_id is None:
        return None
    return settings.users.get(user_id)


def _ensure_active_user(settings: CLISettings) -> tuple[str, StoredUserSettings]:
    user_id = _active_user_id(settings)
    if user_id is None:
        settings.active_user_id = LOCAL_STATE_USER_ID
        settings.users[LOCAL_STATE_USER_ID] = StoredUserSettings()
        return LOCAL_STATE_USER_ID, settings.users[LOCAL_STATE_USER_ID]

    user_settings = settings.users.get(user_id)
    if user_settings is None:
        settings.users[user_id] = StoredUserSettings()
        user_settings = settings.users[user_id]

    settings.active_user_id = user_id
    return user_id, user_settings


def _merge_project_settings(
    source: StoredProjectSettings,
    target: StoredProjectSettings,
) -> StoredProjectSettings:
    return StoredProjectSettings(
        active_project=source.active_project or target.active_project,
        active_api_keys={**target.active_api_keys, **source.active_api_keys},
        llm_proxy_bearer_token=(
            source.llm_proxy_bearer_token or target.llm_proxy_bearer_token
        ),
    )


def get_active_user_id() -> str | None:
    return _active_user_id(load_settings())


def get_active_profile() -> StoredUserProfile | None:
    active_user = _active_user(load_settings())
    if active_user is None:
        return None
    return active_user.profile


def get_active_session() -> StoredSession | None:
    active_user = _active_user(load_settings())
    if active_user is None:
        return None
    return active_user.session


def set_active_session(
    *,
    session: StoredSession | None,
    api_url: str | None = None,
) -> None:
    settings = load_settings()
    _, active_user = _ensure_active_user(settings)
    active_user.session = session
    normalized_api_url = normalize_api_url(api_url)
    if normalized_api_url is not None:
        active_user.api_url = normalized_api_url
    _save_settings(settings)


def set_local_session(
    *,
    session: StoredSession | None,
    api_url: str | None = None,
) -> None:
    settings = load_settings()
    local_settings = settings.users.get(LOCAL_STATE_USER_ID)
    if local_settings is None:
        local_settings = StoredUserSettings()

    local_settings.session = session
    normalized_api_url = normalize_api_url(api_url)
    if normalized_api_url is not None:
        local_settings.api_url = normalized_api_url

    settings.users[LOCAL_STATE_USER_ID] = local_settings
    settings.active_user_id = LOCAL_STATE_USER_ID
    _save_settings(settings)


def clear_active_session() -> None:
    settings = load_settings()
    active_user = _active_user(settings)
    if active_user is None:
        return
    active_user.session = None
    _save_settings(settings)


def get_active_project() -> str | None:
    active_user = _active_user(load_settings())
    if active_user is None:
        return None
    return active_user.project.active_project


def set_active_project(project_id: str | None) -> None:
    settings = load_settings()
    _, active_user = _ensure_active_user(settings)
    active_user.project.active_project = project_id
    _save_settings(settings)


def get_active_api_key(project_id: str) -> str | None:
    active_user = _active_user(load_settings())
    if active_user is None:
        return None
    return active_user.project.active_api_keys.get(project_id)


def set_active_api_key(project_id: str, key: str) -> None:
    settings = load_settings()
    _, active_user = _ensure_active_user(settings)
    active_user.project.active_api_keys[project_id] = key
    _save_settings(settings)


def get_llm_proxy_bearer_token() -> str | None:
    active_user = _active_user(load_settings())
    if active_user is None:
        return None
    return active_user.project.llm_proxy_bearer_token


def set_llm_proxy_bearer_token(token: str | None) -> None:
    settings = load_settings()
    _, active_user = _ensure_active_user(settings)
    active_user.project.llm_proxy_bearer_token = token
    _save_settings(settings)


def get_active_api_url() -> str | None:
    active_user = _active_user(load_settings())
    if active_user is None:
        return None
    return normalize_api_url(active_user.api_url)


def resolve_api_url(*, api_url: str | None = None) -> str:
    explicit_api_url = normalize_api_url(api_url)
    if explicit_api_url is not None:
        return explicit_api_url

    env_api_url = normalize_api_url(os.getenv("MESHAGENT_API_URL"))
    if env_api_url is not None:
        return env_api_url

    active_api_url = get_active_api_url()
    if active_api_url is not None:
        return active_api_url

    return DEFAULT_API_URL


def resolve_pages_domain(*, api_url: str | None = None) -> str:
    return "meshagent.app"


def apply_active_profile_api_url_environment() -> None:
    active_api_url = get_active_api_url()
    if active_api_url is None:
        os.environ.pop(PROFILE_API_URL_ENV, None)
        return

    os.environ[PROFILE_API_URL_ENV] = active_api_url


def save_authenticated_profile(
    *,
    profile: StoredUserProfile,
    session: StoredSession,
    api_url: str,
) -> None:
    settings = load_settings()
    target_settings = settings.users.get(profile.id)
    if target_settings is None:
        target_settings = StoredUserSettings()

    local_settings = settings.users.get(LOCAL_STATE_USER_ID)
    if local_settings is not None and profile.id != LOCAL_STATE_USER_ID:
        target_settings.project = _merge_project_settings(
            local_settings.project,
            target_settings.project,
        )
        if target_settings.api_url is None:
            target_settings.api_url = local_settings.api_url
        del settings.users[LOCAL_STATE_USER_ID]

    target_settings.profile = profile
    target_settings.session = session
    target_settings.api_url = normalize_api_url(api_url)
    settings.users[profile.id] = target_settings
    settings.active_user_id = profile.id
    _save_settings(settings)


def list_saved_profiles() -> list[SavedProfileRecord]:
    settings = load_settings()
    active_user_id = _active_user_id(settings)
    profiles: list[SavedProfileRecord] = []

    for user_id, user_settings in settings.users.items():
        if user_id == LOCAL_STATE_USER_ID:
            continue
        if user_settings.profile is None or user_settings.session is None:
            continue
        if user_settings.session.access_token is None:
            continue

        profiles.append(
            SavedProfileRecord(
                user_id=user_id,
                profile=user_settings.profile,
                api_url=normalize_api_url(user_settings.api_url),
                is_active=user_id == active_user_id,
            )
        )

    profiles.sort(
        key=lambda profile: (
            not profile.is_active,
            profile.profile.email or profile.user_id,
        )
    )
    return profiles


def switch_active_profile(selector: str) -> SavedProfileRecord:
    normalized_selector = selector.strip()
    if normalized_selector == "":
        raise LookupError("Profile selector cannot be empty.")

    profiles = list_saved_profiles()
    exact_id_matches = [
        profile for profile in profiles if profile.user_id == normalized_selector
    ]
    if len(exact_id_matches) == 1:
        selected = exact_id_matches[0]
    else:
        email_matches = [
            profile
            for profile in profiles
            if profile.profile.email is not None
            and profile.profile.email.lower() == normalized_selector.lower()
        ]
        if len(email_matches) == 0:
            raise LookupError(
                f"No saved local profile matches '{normalized_selector}'."
            )
        if len(email_matches) > 1:
            raise LookupError(
                f"More than one saved local profile matches '{normalized_selector}'. "
                "Use the user id instead."
            )
        selected = email_matches[0]

    settings = load_settings()
    settings.active_user_id = selected.user_id
    _save_settings(settings)
    apply_active_profile_api_url_environment()
    return SavedProfileRecord(
        user_id=selected.user_id,
        profile=selected.profile,
        api_url=selected.api_url,
        is_active=True,
    )
