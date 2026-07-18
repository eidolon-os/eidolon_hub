from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eidolon_data import DataSettings, DataStore
from eidolon_data.adapters import EidolonDataDeviceRegistryRepository
from eidolon_sdk.biz.devices import body_sha256_hex, canonical_request, public_key_fingerprint
from fastapi import FastAPI

from hub.api.routers.system.guard_owner_face import router
from hub.core.device_manager import DeviceManager
from hub.core.guard_owner_face_profile_reconciler import GuardOwnerFaceProfileReconciler


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.command_results: list[dict] = []
        self.commands_by_id: dict[str, dict] = {}
        self.online = True

    async def send_command(self, device_id: str, payload: dict, **kwargs):
        if not self.online:
            raise ValueError(f"Device {device_id} is not currently connected")
        self.calls.append({"device_id": device_id, "payload": payload, **kwargs})
        command_id = kwargs.get("command_id") or f"cmd-face-{len(self.calls)}"
        command = {"command_id": command_id, "status": "sent"}
        self.commands_by_id[command_id] = command
        return command

    async def get_command(self, command_id: str):
        return self.commands_by_id.get(command_id)

    async def list_commands(self, limit: int = 50):
        return self.command_results[:limit]


@pytest.fixture
async def context(tmp_path):
    store = DataStore.open(
        DataSettings(
            sqlite_path=str(tmp_path / "owner-face.sqlite3"),
            object_store_path=str(tmp_path / "objects"),
        )
    )
    await store.init_schema()
    manager = DeviceManager(EidolonDataDeviceRegistryRepository(store))
    await manager.load()
    app = FastAPI()
    app.state.data_store = store
    app.state.device_manager = manager
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield store, manager, client
    await store.close()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


async def _approved_device(manager: DeviceManager, device_id: str):
    key = ec.generate_private_key(ec.SECP256R1())
    public_key = _b64url(
        key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    )
    await manager.register_signed_seen(
        device_id=device_id,
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        nonce=f"bootstrap-{device_id}",
        guard_manifest={"enabled": True, "protocol_versions": [1]},
    )
    await manager.approve(device_id)
    return key


def _signed_headers(key, *, device_id: str, path: str, nonce: str) -> dict[str, str]:
    timestamp = "0"
    signed = canonical_request(
        method="GET",
        path_query=path,
        device_id=device_id,
        nonce=nonce,
        timestamp=timestamp,
        body_hash=body_sha256_hex(),
    )
    return {
        "X-Device-ID": device_id,
        "X-Device-Nonce": nonce,
        "X-Device-Timestamp": timestamp,
        "X-Device-Signature": _b64url(key.sign(signed, ec.ECDSA(hashes.SHA256()))),
    }


async def _desired_profile(store: DataStore, owner_id: str):
    profile = await store.owner_face_profiles.create_draft(
        owner_id=owner_id,
        model_id="esp-who-human-face-recognition-v1",
        preprocessing_version="rgb565-be-qvga-v1",
    )
    references = []
    for pose in ("front", "left", "right"):
        content = f"normalized-jpeg-{owner_id}-{pose}".encode()
        digest = hashlib.sha256(content).hexdigest()
        key = f"{owner_id}/{profile.profile_id}/{pose}.jpg"
        store.object_storage.put(key, content, expected_sha256=digest)
        references.append(
            await store.owner_face_profiles.add_reference(
                profile_revision_id=profile.profile_revision_id,
                pose=pose,
                content_type="image/jpeg",
                size_bytes=len(content),
                sha256=digest,
                storage_key=key,
            )
        )
    return await store.owner_face_profiles.activate(profile.profile_revision_id), references


async def test_signed_manifest_and_reference_are_scoped_to_active_binding(context) -> None:
    store, manager, client = context
    await store.owners.create(owner_id="owner-1", display_name="Owner")
    key = await _approved_device(manager, "atk-1")
    binding = await store.guard_bindings.claim(
        owner_id="owner-1", device_id="atk-1", guard_companion_id="guard-1"
    )
    profile, references = await _desired_profile(store, "owner-1")

    manifest_path = "/api/guard/owner-face-profile"
    response = await client.get(
        manifest_path,
        headers=_signed_headers(
            key, device_id="atk-1", path=manifest_path, nonce="manifest-1"
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["binding_id"] == binding.binding_id
    assert body["profile_id"] == profile.profile_id
    assert body["profile_revision"] == 1
    assert [item["pose"] for item in body["references"]] == ["front", "left", "right"]
    assert all("url" not in item and "storage_key" not in item for item in body["references"])

    reference_path = f"/api/guard/owner-face-references/{references[0].reference_id}"
    media = await client.get(
        reference_path,
        headers=_signed_headers(
            key, device_id="atk-1", path=reference_path, nonce="reference-1"
        ),
    )
    assert media.status_code == 200
    assert media.headers["cache-control"] == "no-store"
    assert media.headers["content-type"].startswith("image/jpeg")
    assert hashlib.sha256(media.content).hexdigest() == body["references"][0]["sha256"]

    replay = await client.get(
        reference_path,
        headers=_signed_headers(
            key, device_id="atk-1", path=reference_path, nonce="reference-1"
        ),
    )
    assert replay.status_code == 409


async def test_reference_from_another_owner_is_not_disclosed(context) -> None:
    store, manager, client = context
    await store.owners.create(owner_id="owner-1", display_name="One")
    await store.owners.create(owner_id="owner-2", display_name="Two")
    owner1_key = await _approved_device(manager, "atk-1")
    owner2_key = await _approved_device(manager, "atk-2")
    await store.guard_bindings.claim(
        owner_id="owner-1", device_id="atk-1", guard_companion_id="guard-1"
    )
    await store.guard_bindings.claim(
        owner_id="owner-2", device_id="atk-2", guard_companion_id="guard-2"
    )
    _profile, owner1_references = await _desired_profile(store, "owner-1")
    await _desired_profile(store, "owner-2")
    reference_path = (
        f"/api/guard/owner-face-references/{owner1_references[0].reference_id}"
    )

    denied = await client.get(
        reference_path,
        headers=_signed_headers(
            owner2_key,
            device_id="atk-2",
            path=reference_path,
            nonce="cross-owner",
        ),
    )
    assert denied.status_code == 404

    allowed = await client.get(
        reference_path,
        headers=_signed_headers(
            owner1_key,
            device_id="atk-1",
            path=reference_path,
            nonce="own-reference",
        ),
    )
    assert allowed.status_code == 200


async def test_profile_reconciler_retries_offline_and_records_apply(context) -> None:
    store, manager, _client = context
    await store.owners.create(owner_id="owner-1", display_name="Owner")
    await _approved_device(manager, "atk-1")
    binding = await store.guard_bindings.claim(
        owner_id="owner-1", device_id="atk-1", guard_companion_id="guard-1"
    )
    profile, _references = await _desired_profile(store, "owner-1")
    runtime = _Runtime()
    runtime.online = False
    reconciler = GuardOwnerFaceProfileReconciler(  # type: ignore[arg-type]
        store, runtime, retry_base_seconds=0
    )

    assert await reconciler.reconcile_once() == 0
    delivery = (
        await store.guard_owner_face_profile_deliveries.list_for_binding(
            binding.binding_id
        )
    )[0]
    assert delivery.attempt_count == 0
    runtime.online = True
    assert await reconciler.reconcile_once() == 1
    assert runtime.calls[0]["op"] == "guard.owner_face_profile.sync"
    assert runtime.calls[0]["payload"] == {
        "binding_id": binding.binding_id,
        "profile_id": profile.profile_id,
        "profile_revision": 1,
        "desired_state": "active",
    }

    await reconciler.apply_command_result(
        {
            "command_id": runtime.calls[0]["command_id"],
            "op": "guard.owner_face_profile.sync",
            "status": "succeeded",
            "result": {
                "binding_id": binding.binding_id,
                "profile_id": profile.profile_id,
                "profile_revision": 1,
                "applied_state": "active",
                "model_id": "esp-who-human-face-recognition-v1",
                "preprocessing_version": "rgb565-be-qvga-v1",
                "template_count": 3,
            },
        }
    )
    deliveries = await store.guard_owner_face_profile_deliveries.list_for_binding(
        binding.binding_id
    )
    assert deliveries[0].status == "applied"
    events = await store.events.list_for_owner("owner-1")
    assert events[0].event_type == "guard.owner_face_profile.applied"

    # Periodic reconciliation scans persisted terminal commands. Replaying the
    # same command must not rewrite an already-terminal delivery or emit the
    # deterministic audit event twice.
    await reconciler.apply_command_result(
        {
            "command_id": runtime.calls[0]["command_id"],
            "op": "guard.owner_face_profile.sync",
            "status": "succeeded",
            "result": {
                "binding_id": binding.binding_id,
                "profile_id": profile.profile_id,
                "profile_revision": 1,
                "applied_state": "active",
                "model_id": "esp-who-human-face-recognition-v1",
                "preprocessing_version": "rgb565-be-qvga-v1",
                "template_count": 3,
            },
        }
    )
    assert len(await store.events.list_for_owner("owner-1")) == 1


async def test_profile_reconciler_requeues_no_ack_timeout_without_burning_attempt(
    context,
) -> None:
    store, manager, _client = context
    await store.owners.create(owner_id="owner-timeout", display_name="Owner")
    await _approved_device(manager, "atk-timeout")
    binding = await store.guard_bindings.claim(
        owner_id="owner-timeout",
        device_id="atk-timeout",
        guard_companion_id="guard-timeout",
    )
    await _desired_profile(store, "owner-timeout")
    runtime = _Runtime()
    reconciler = GuardOwnerFaceProfileReconciler(  # type: ignore[arg-type]
        store, runtime, retry_base_seconds=0
    )

    assert await reconciler.reconcile_once() == 1
    runtime.command_results = [
        {
            "command_id": runtime.calls[0]["command_id"],
            "op": "guard.owner_face_profile.sync",
            "status": "timeout",
            "error": "no result within 300s",
        }
    ]

    assert await reconciler.reconcile_command_results() == 1
    delivery = (
        await store.guard_owner_face_profile_deliveries.list_for_binding(binding.binding_id)
    )[0]
    assert delivery.status == "pending"
    assert delivery.attempt_count == 0
    assert await reconciler.reconcile_command_results() == 0


async def test_profile_reconciler_accepted_timeout_consumes_device_attempt(context) -> None:
    store, manager, _client = context
    await store.owners.create(owner_id="owner-accepted-timeout", display_name="Owner")
    await _approved_device(manager, "atk-accepted-timeout")
    binding = await store.guard_bindings.claim(
        owner_id="owner-accepted-timeout",
        device_id="atk-accepted-timeout",
        guard_companion_id="guard-accepted-timeout",
    )
    await _desired_profile(store, "owner-accepted-timeout")
    runtime = _Runtime()
    reconciler = GuardOwnerFaceProfileReconciler(  # type: ignore[arg-type]
        store, runtime, retry_base_seconds=0
    )

    assert await reconciler.reconcile_once() == 1
    runtime.command_results = [
        {
            "command_id": runtime.calls[0]["command_id"],
            "op": "guard.owner_face_profile.sync",
            "status": "timeout",
            "error": "no result within 300s",
            "ack": {"kind": "ack", "status": "accepted"},
        }
    ]

    assert await reconciler.reconcile_command_results() == 1
    delivery = (
        await store.guard_owner_face_profile_deliveries.list_for_binding(
            binding.binding_id
        )
    )[0]
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1


async def test_profile_reconciler_applies_clear_and_retries_transient_failure(context) -> None:
    store, manager, _client = context
    await store.owners.create(owner_id="owner-clear", display_name="Owner")
    await _approved_device(manager, "atk-clear")
    binding = await store.guard_bindings.claim(
        owner_id="owner-clear", device_id="atk-clear", guard_companion_id="guard-clear"
    )
    profile, _references = await _desired_profile(store, "owner-clear")
    cleared = await store.owner_face_profiles.clear(owner_id="owner-clear")
    runtime = _Runtime()
    reconciler = GuardOwnerFaceProfileReconciler(  # type: ignore[arg-type]
        store, runtime, retry_base_seconds=0
    )

    assert await reconciler.reconcile_once() == 1
    assert runtime.calls[0]["payload"] == {
        "binding_id": binding.binding_id,
        "profile_id": profile.profile_id,
        "profile_revision": cleared.revision,
        "desired_state": "cleared",
    }
    await reconciler.apply_command_result(
        {
            "command_id": runtime.calls[0]["command_id"],
            "op": "guard.owner_face_profile.sync",
            "status": "failed",
            "error": "OWNER_FACE_MANIFEST_FETCH_FAILED",
        }
    )
    deliveries = await store.guard_owner_face_profile_deliveries.list_for_binding(
        binding.binding_id
    )
    clear_delivery = next(row for row in deliveries if row.desired_state == "cleared")
    assert clear_delivery.status == "pending"

    assert await reconciler.reconcile_once() == 1
    await reconciler.apply_command_result(
        {
            "command_id": runtime.calls[1]["command_id"],
            "op": "guard.owner_face_profile.sync",
            "status": "succeeded",
            "result": {
                "binding_id": binding.binding_id,
                "profile_id": profile.profile_id,
                "profile_revision": cleared.revision,
                "applied_state": "cleared",
                "model_id": None,
                "preprocessing_version": None,
                "template_count": 0,
            },
        }
    )
    deliveries = await store.guard_owner_face_profile_deliveries.list_for_binding(
        binding.binding_id
    )
    clear_delivery = next(row for row in deliveries if row.desired_state == "cleared")
    assert clear_delivery.status == "applied"
    events = await store.events.list_for_owner("owner-clear")
    assert events[0].event_type == "guard.owner_face_profile.applied"


async def test_profile_reconciler_rejects_malformed_success_result(context) -> None:
    store, manager, _client = context
    await store.owners.create(owner_id="owner-bad-result", display_name="Owner")
    await _approved_device(manager, "atk-bad-result")
    binding = await store.guard_bindings.claim(
        owner_id="owner-bad-result",
        device_id="atk-bad-result",
        guard_companion_id="guard-bad-result",
    )
    profile, _references = await _desired_profile(store, "owner-bad-result")
    runtime = _Runtime()
    reconciler = GuardOwnerFaceProfileReconciler(  # type: ignore[arg-type]
        store, runtime, retry_base_seconds=0
    )

    assert await reconciler.reconcile_once() == 1
    await reconciler.apply_command_result(
        {
            "command_id": runtime.calls[0]["command_id"],
            "op": "guard.owner_face_profile.sync",
            "status": "succeeded",
            "result": {
                "binding_id": binding.binding_id,
                "profile_id": profile.profile_id,
                "profile_revision": profile.revision,
                "applied_state": "active",
                "model_id": "esp-who-human-face-recognition-v1",
                "preprocessing_version": "rgb565-be-qvga-v1",
                "template_count": 0,
            },
        }
    )
    delivery = (
        await store.guard_owner_face_profile_deliveries.list_for_binding(binding.binding_id)
    )[0]
    assert delivery.status == "failed"
    assert delivery.last_error == "owner face profile result is invalid"
    events = await store.events.list_for_owner("owner-bad-result")
    assert events[0].event_type == "guard.owner_face_profile.failed"


async def test_profile_reconciler_recovers_inflight_command_without_resending(
    context,
) -> None:
    store, manager, _client = context
    await store.owners.create(owner_id="owner-crash", display_name="Owner")
    await _approved_device(manager, "atk-crash")
    binding = await store.guard_bindings.claim(
        owner_id="owner-crash",
        device_id="atk-crash",
        guard_companion_id="guard-crash",
    )
    await _desired_profile(store, "owner-crash")
    delivery = (
        await store.guard_owner_face_profile_deliveries.list_for_binding(binding.binding_id)
    )[0]
    claimed = await store.guard_owner_face_profile_deliveries.claim_for_dispatch(
        delivery.delivery_id,
        command_id="cmd-before-hub-restart",
        lease_seconds=0,
    )
    assert claimed is not None

    runtime = _Runtime()
    runtime.commands_by_id["cmd-before-hub-restart"] = {
        "command_id": "cmd-before-hub-restart",
        "status": "accepted",
    }
    reconciler = GuardOwnerFaceProfileReconciler(  # type: ignore[arg-type]
        store, runtime, retry_base_seconds=0
    )

    assert await reconciler.reconcile_once() == 1
    assert runtime.calls == []
    recovered = (
        await store.guard_owner_face_profile_deliveries.list_for_binding(binding.binding_id)
    )[0]
    assert recovered.status == "dispatched"
    assert recovered.command_id == "cmd-before-hub-restart"
    assert recovered.attempt_count == 1
    assert await reconciler.reconcile_once() == 0
