"""Device-management service graph assembly."""

from __future__ import annotations

from datetime import timedelta

from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.management_jwt import JwtOwnerManagementAuthorizer
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.queries.get_device import GetDevice
from hub.application.queries.list_devices import ListDevices
from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.interfaces.http.routers.device_management import DeviceManagementHttpServices
from hub.ports.channels import ChannelProviderControl
from hub.ports.identity import Clock
from hub.ports.repositories import DeviceDirectoryRepository


def build_device_management(
    *,
    repositories: SqlHubRepositories,
    directory: DeviceDirectoryRepository,
    projector: ProjectDeviceDirectory,
    provider: ChannelProviderControl,
    hub_id: str,
    management_jwt_secret: bytes,
    clock: Clock,
    handoff_ttl: timedelta,
) -> DeviceManagementHttpServices:
    return DeviceManagementHttpServices(
        get_device=GetDevice(directory),
        list_devices=ListDevices(directory),
        approve_device=ApproveDevice(
            devices=repositories.devices,
            events=repositories.management_events,
            clock=clock,
            handoff_ttl=handoff_ttl,
            directory_projector=projector,
        ),
        revoke_device=RevokeDevice(
            devices=repositories.devices,
            provider=provider,
            hub_id=hub_id,
            events=repositories.management_events,
            clock=clock,
            directory_projector=projector,
        ),
        authorizer=JwtOwnerManagementAuthorizer(
            secret=management_jwt_secret,
            devices=repositories.devices,
        ),
        event_stream=repositories.management_events,
    )
