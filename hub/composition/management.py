"""Device-management service graph assembly."""

from __future__ import annotations

from hub.adapters.channels.data_bridge import ProviderDataChannelBridge
from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.management_jwt import JwtOwnerManagementAuthorizer
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.use_cases.approve_device import ApproveDevice
from hub.application.use_cases.get_command import GetCommand
from hub.application.use_cases.revoke_device import RevokeDevice
from hub.application.use_cases.send_command import SendCommand
from hub.interfaces.http.routers.device_management import DeviceManagementHttpServices
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import DeviceDirectoryRepository


def build_device_management(
    *,
    repositories: SqlHubRepositories,
    directory: DeviceDirectoryRepository,
    projector: ProjectDeviceDirectory,
    bridge: ProviderDataChannelBridge,
    management_jwt_secret: bytes,
    clock: Clock,
    ids: IdGenerator,
) -> DeviceManagementHttpServices:
    return DeviceManagementHttpServices(
        directory=directory,
        send_command=SendCommand(
            devices=repositories.devices,
            sessions=repositories.sessions,
            commands=repositories.commands,
            sender=bridge,
            clock=clock,
            ids=ids,
        ),
        get_command=GetCommand(repositories.commands),
        approve_device=ApproveDevice(
            devices=repositories.devices,
            events=repositories.events,
            clock=clock,
            directory_projector=projector,
        ),
        revoke_device=RevokeDevice(
            devices=repositories.devices,
            sessions=repositories.sessions,
            events=repositories.events,
            clock=clock,
            directory_projector=projector,
        ),
        authorizer=JwtOwnerManagementAuthorizer(
            secret=management_jwt_secret,
            devices=repositories.devices,
        ),
        event_stream=repositories.events,
    )
