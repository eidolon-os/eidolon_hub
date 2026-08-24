"""Device-management service graph assembly."""

from __future__ import annotations

from hub.adapters.persistence.repositories import SqlHubRepositories
from hub.adapters.security.management_jwt import JwtOwnerManagementAuthorizer
from hub.application.projections.device_directory import ProjectDeviceDirectory
from hub.application.queries.get_device import GetDevice
from hub.application.queries.list_devices import ListDevices
from hub.application.use_cases.rename_device import RenameDevice
from hub.interfaces.http.routers.device_management import DeviceManagementHttpServices
from hub.ports.identity import Clock, IdGenerator
from hub.ports.repositories import DeviceDirectoryRepository


def build_device_management(
    *,
    repositories: SqlHubRepositories,
    directory: DeviceDirectoryRepository,
    projector: ProjectDeviceDirectory,
    management_jwt_secret: bytes,
    device_registry_reader_token: str,
    clock: Clock,
    ids: IdGenerator,
) -> DeviceManagementHttpServices:
    del ids
    return DeviceManagementHttpServices(
        get_device=GetDevice(directory),
        list_devices=ListDevices(directory, clock=clock),
        rename_device=RenameDevice(
            devices=repositories.devices,
            mutations=repositories.device_mutations,
            clock=clock,
        ),
        authorizer=JwtOwnerManagementAuthorizer(
            secret=management_jwt_secret,
            devices=repositories.devices,
            device_registry_reader_token=device_registry_reader_token,
        ),
        event_stream=repositories.management_events,
    )
