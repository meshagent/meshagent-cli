import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

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
    # Retain the on-disk field name; its value is now an environment/profile key.
    active_user_id: str | None = None
    users: dict[str, StoredUserSettings] = Field(default_factory=dict)
    active_profiles: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SavedProfileRecord:
    user_id: str
    profile: StoredUserProfile
    api_url: str | None
    is_active: bool

    @property
    def key(self) -> str:
        return profile_key(self.user_id, self.api_url)


def profile_key(user_id: str, api_url: str | None) -> str:
    return json.dumps(
        [normalize_api_url(api_url) or DEFAULT_API_URL, user_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )


_PROFILE_KEY = TypeAdapter(tuple[str, str])


def _stored_user_id(key: str, user: StoredUserSettings) -> str:
    if user.profile is not None:
        return user.profile.id
    try:
        _, user_id = _PROFILE_KEY.validate_json(key)
        return user_id
    except ValidationError:
        return key


def _upgrade_settings(settings: CLISettings) -> bool:
    original = settings.model_copy(deep=True)
    users: dict[str, StoredUserSettings] = {}
    aliases: dict[str, str] = {}
    active_profiles: dict[str, str] = {}
    # Existing scoped records take precedence if an older CLI added a legacy
    # record to an upgraded file. Merge project settings without losing keys.
    for key, user in sorted(
        settings.users.items(),
        key=lambda item: (
            item[0] == profile_key(_stored_user_id(*item), item[1].api_url),
            item[0],
        ),
    ):
        api_url = normalize_api_url(user.api_url) or DEFAULT_API_URL
        user_id = _stored_user_id(key, user)
        scoped_key = profile_key(user_id, api_url)
        aliases[key] = scoped_key
        user.api_url = api_url
        previous = users.get(scoped_key)
        if previous is not None:
            user = user.model_copy(
                update={
                    "profile": user.profile or previous.profile,
                    "session": user.session or previous.session,
                    "project": _merge_project_settings(user.project, previous.project),
                }
            )
        users[scoped_key] = user
        active_profiles.setdefault(api_url, scoped_key)
    for api_url, key in settings.active_profiles.items():
        api_url = normalize_api_url(api_url) or DEFAULT_API_URL
        key = aliases.get(key, key)
        if key in users and users[key].api_url == api_url:
            active_profiles[api_url] = key
    active_key = aliases.get(settings.active_user_id or "")
    if active_key is None and len(users) == 1:
        active_key = next(iter(users))
    if active_key is not None:
        active_profiles[users[active_key].api_url or DEFAULT_API_URL] = active_key
    settings.users = users
    settings.active_user_id = active_key
    settings.active_profiles = active_profiles
    return settings != original


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
    if (
        settings.active_user_id is not None
        and settings.active_user_id not in settings.users
    ):
        settings.active_user_id = None


def _save_settings(settings: CLISettings) -> None:
    _cleanup_settings(settings)
    _ensure_settings_dir()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=SETTINGS_DIR, delete=False
        ) as file:
            temporary_path = Path(file.name)
            json.dump(settings.model_dump(mode="json"), file, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, SETTINGS_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
            settings = CLISettings.model_validate_json(SETTINGS_FILE.read_text())
            if _upgrade_settings(settings):
                try:
                    _save_settings(settings)
                except OSError as ex:
                    if ex.errno not in (1, 13, 30):
                        raise
            return settings

        migrated = _migrate_legacy_settings()
        if migrated is None:
            return CLISettings()

        _upgrade_settings(migrated)
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


def _active_user_id(settings: CLISettings, api_url: str | None = None) -> str | None:
    environment = normalize_api_url(api_url) or normalize_api_url(
        os.getenv("MESHAGENT_API_URL")
    )
    if environment is not None:
        key = settings.active_profiles.get(environment)
        return key if key in settings.users else None
    if (
        settings.active_user_id is not None
        and settings.active_user_id in settings.users
    ):
        return settings.active_user_id

    if len(settings.users) == 1:
        return next(iter(settings.users))

    return None


def _active_user(settings: CLISettings) -> StoredUserSettings | None:
    user_id = _active_user_id(settings)
    if user_id is None:
        return None
    return settings.users.get(user_id)


def _select_profile(settings: CLISettings, key: str) -> None:
    settings.active_user_id = key
    settings.active_profiles[settings.users[key].api_url or DEFAULT_API_URL] = key


def _ensure_active_user(
    settings: CLISettings, api_url: str | None = None
) -> tuple[str, StoredUserSettings]:
    user_id = _active_user_id(settings, api_url)
    if user_id is None:
        environment = (
            normalize_api_url(api_url)
            or normalize_api_url(os.getenv("MESHAGENT_API_URL"))
            or DEFAULT_API_URL
        )
        user_id = profile_key(LOCAL_STATE_USER_ID, environment)
        settings.users.setdefault(user_id, StoredUserSettings(api_url=environment))

    user_settings = settings.users.get(user_id)
    if user_settings is None:
        settings.users[user_id] = StoredUserSettings()
        user_settings = settings.users[user_id]

    _select_profile(settings, user_id)
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
    settings = load_settings()
    key = _active_user_id(settings)
    return _stored_user_id(key, settings.users[key]) if key is not None else None


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
    _, active_user = _ensure_active_user(settings, api_url)
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
    normalized_api_url = normalize_api_url(api_url) or resolve_api_url()
    key = profile_key(LOCAL_STATE_USER_ID, normalized_api_url)
    local_settings = settings.users.get(key)
    if local_settings is None:
        local_settings = StoredUserSettings()

    local_settings.session = session
    local_settings.api_url = normalized_api_url

    settings.users[key] = local_settings
    _select_profile(settings, key)
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
    api_url = normalize_api_url(api_url) or DEFAULT_API_URL
    key = profile_key(profile.id, api_url)
    target_settings = settings.users.get(key)
    if target_settings is None:
        target_settings = StoredUserSettings()

    local_key = profile_key(LOCAL_STATE_USER_ID, api_url)
    local_settings = settings.users.get(local_key)
    if local_settings is not None and profile.id != LOCAL_STATE_USER_ID:
        target_settings.project = _merge_project_settings(
            local_settings.project,
            target_settings.project,
        )
        if target_settings.api_url is None:
            target_settings.api_url = local_settings.api_url
        del settings.users[local_key]

    target_settings.profile = profile
    target_settings.session = session
    target_settings.api_url = normalize_api_url(api_url)
    settings.users[key] = target_settings
    _select_profile(settings, key)
    _save_settings(settings)


def list_saved_profiles() -> list[SavedProfileRecord]:
    settings = load_settings()
    active_user_id = _active_user_id(settings)
    profiles: list[SavedProfileRecord] = []

    for key, user_settings in settings.users.items():
        if _stored_user_id(key, user_settings) == LOCAL_STATE_USER_ID:
            continue
        if user_settings.profile is None or user_settings.session is None:
            continue
        if user_settings.session.access_token is None:
            continue

        profiles.append(
            SavedProfileRecord(
                user_id=user_settings.profile.id,
                profile=user_settings.profile,
                api_url=normalize_api_url(user_settings.api_url),
                is_active=key == active_user_id,
            )
        )

    profiles.sort(
        key=lambda profile: (
            not profile.is_active,
            profile.profile.email or profile.user_id,
        )
    )
    return profiles


def switch_active_profile(
    selector: str, *, api_url: str | None = None
) -> SavedProfileRecord:
    normalized_selector = selector.strip()
    if normalized_selector == "":
        raise LookupError("Profile selector cannot be empty.")

    profiles = list_saved_profiles()
    explicit_environment = normalize_api_url(api_url)
    if explicit_environment is not None:
        profiles = [
            profile for profile in profiles if profile.api_url == explicit_environment
        ]
    environment = explicit_environment or normalize_api_url(
        os.getenv("MESHAGENT_API_URL")
    )
    exact_keys = [profile for profile in profiles if profile.key == normalized_selector]
    if not exact_keys and environment is not None:
        profiles = [profile for profile in profiles if profile.api_url == environment]
    exact_id_matches = [
        profile for profile in profiles if profile.user_id == normalized_selector
    ]
    if len(exact_keys) == 1:
        selected = exact_keys[0]
    elif len(exact_id_matches) == 1:
        selected = exact_id_matches[0]
    else:
        email_matches = exact_id_matches or [
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
                "Use --api-url to select the environment, or select a unique user id."
            )
        selected = email_matches[0]

    settings = load_settings()
    _select_profile(settings, selected.key)
    _save_settings(settings)
    apply_active_profile_api_url_environment()
    return SavedProfileRecord(
        user_id=selected.user_id,
        profile=selected.profile,
        api_url=selected.api_url,
        is_active=True,
    )
