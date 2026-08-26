"""Device-management service graph assembly."""

from __future__ import annotations

from hub.adapters.security.device_registry_reader import DeviceRegistryReaderAuthorizer
from hub.application.queries.get_device import GetDevice
from hub.interfaces.http.routers.device_management import DeviceManagementHttpServices
from hub.ports.repositories import DeviceDirectoryRepository


def build_device_management(
    *,
    directory: DeviceDirectoryRepository,
    device_registry_reader_token: str,
) -> DeviceManagementHttpServices:
    return DeviceManagementHttpServices(
        get_device=GetDevice(directory),
        authorizer=DeviceRegistryReaderAuthorizer(
            device_registry_reader_token=device_registry_reader_token,
        ),
    )
