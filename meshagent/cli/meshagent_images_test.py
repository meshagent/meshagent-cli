from __future__ import annotations

from meshagent.cli import meshagent_images


def test_meshagent_image_prefix_prefers_explicit_environment(monkeypatch) -> None:
    monkeypatch.setenv("MESHAGENT_IMAGE_PREFIX", "registry.example.com/custom")
    monkeypatch.setattr(meshagent_images, "get_active_api_url", lambda: None)

    assert meshagent_images.meshagent_image_prefix() == "registry.example.com/custom/"


def test_meshagent_image_prefix_uses_dev_registry_for_life_profile(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MESHAGENT_IMAGE_PREFIX", raising=False)
    monkeypatch.delenv("MESHAGENT_API_URL", raising=False)
    monkeypatch.setattr(
        meshagent_images,
        "get_active_api_url",
        lambda: "https://api.meshagent.life",
    )

    assert meshagent_images.meshagent_image_prefix() == (
        "us-central1-docker.pkg.dev/meshagent-life/meshagent-public/"
    )


def test_meshagent_image_prefix_uses_prod_registry_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MESHAGENT_IMAGE_PREFIX", raising=False)
    monkeypatch.delenv("MESHAGENT_API_URL", raising=False)
    monkeypatch.setattr(meshagent_images, "get_active_api_url", lambda: None)

    assert meshagent_images.meshagent_image_prefix() == (
        "us-central1-docker.pkg.dev/meshagent-public/images/"
    )
