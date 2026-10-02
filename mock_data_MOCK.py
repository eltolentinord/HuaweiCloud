# coding: utf-8
"""
==============================================================================
MOCK_DATA_MOCK.py
==============================================================================
DATOS SIMULADOS SOLO PARA VERIFICACIÓN VISUAL DEL DISEÑO.

- NO se importa en main.py ni en inventory.py.
- NO se usan en producción.
- Los IPs, IDs, nombres y credenciales son inventados.
==============================================================================
"""

MOCK_RESPONSE = {
    "service": "ecs",
    "region": "ap-southeast-3",
    "project_id_masked": "mock00…0000",
    "resumen": [
        {"label": "Total ECS", "value": 5},
        {"label": "ECS activas", "value": 3},
        {"label": "ECS apagadas", "value": 1},
        {"label": "Total vCPU", "value": 18},
        {"label": "Total RAM (GB)", "value": 40},
    ],
    "errores": [],
    "tables": [
        {
            "titulo": "ECS",
            "columnas": [
                "Nombre ECS", "Estado", "Región", "Zona AZ", "Flavor",
                "vCPU", "RAM (GB)", "IP privada", "IP pública",
                "Security Group", "Sistema operativo", "Fecha de creación",
            ],
            "filas": [
                {"Nombre ECS": "web-prod-01", "Estado": "ACTIVE", "Región": "ap-southeast-3", "Zona AZ": "ap-southeast-3a", "Flavor": "s6.large.2", "vCPU": 2, "RAM (GB)": 4, "IP privada": "192.168.0.10", "IP pública": "47.88.10.21", "Security Group": "web-sg", "Sistema operativo": "image-ubuntu-22.04", "Fecha de creación": "2026-01-15T10:22:00Z",
                 "_detalle": {"ECS ID": "mock-ecs-0001", "Tenant ID": "mock-tenant", "User ID": "mock-user", "Enterprise Project ID": "mock-ep", "Imagen": "image-ubuntu-22.04", "Flavor": "s6.large.2", "Redes": "vpc-01", "Todas las IP": "192.168.0.10, 47.88.10.21", "MAC address": "fa:16:3e:aa:bb:cc", "Security Groups": "web-sg", "Volume IDs": "vol-0001", "Tags": "env=prod", "Metadata": "{}", "Fecha de creación": "2026-01-15T10:22:00Z", "Fecha de actualización": "2026-01-15T10:30:00Z"}},
                {"Nombre ECS": "db-main", "Estado": "ACTIVE", "Región": "ap-southeast-3", "Zona AZ": "ap-southeast-3a", "Flavor": "c6.xlarge.2", "vCPU": 4, "RAM (GB)": 16, "IP privada": "192.168.0.20", "IP pública": "-", "Security Group": "db-sg", "Sistema operativo": "image-rocky-9", "Fecha de creación": "2026-02-03T08:11:00Z",
                 "_detalle": {"ECS ID": "mock-ecs-0002", "Tenant ID": "mock-tenant", "User ID": "mock-user", "Enterprise Project ID": "mock-ep", "Imagen": "image-rocky-9", "Flavor": "c6.xlarge.2", "Redes": "vpc-01", "Todas las IP": "192.168.0.20", "MAC address": "fa:16:3e:aa:bb:cd", "Security Groups": "db-sg", "Volume IDs": "vol-0002", "Tags": "env=prod", "Metadata": "{}", "Fecha de creación": "2026-02-03T08:11:00Z", "Fecha de actualización": "2026-02-03T08:11:00Z"}},
                {"Nombre ECS": "bastion", "Estado": "SHUTOFF", "Región": "ap-southeast-3", "Zona AZ": "ap-southeast-3b", "Flavor": "s6.medium.2", "vCPU": 1, "RAM (GB)": 2, "IP privada": "192.168.1.5", "IP pública": "47.88.10.99", "Security Group": "bastion-sg", "Sistema operativo": "image-debian-12", "Fecha de creación": "2025-12-20T14:00:00Z",
                 "_detalle": {"ECS ID": "mock-ecs-0003", "Tenant ID": "mock-tenant", "User ID": "mock-user", "Enterprise Project ID": "mock-ep", "Imagen": "image-debian-12", "Flavor": "s6.medium.2", "Redes": "vpc-01", "Todas las IP": "192.168.1.5, 47.88.10.99", "MAC address": "fa:16:3e:aa:bb:ce", "Security Groups": "bastion-sg", "Volume IDs": "vol-0003", "Tags": "-", "Metadata": "{}", "Fecha de creación": "2025-12-20T14:00:00Z", "Fecha de actualización": "2026-03-01T09:00:00Z"}},
                {"Nombre ECS": "batch-worker", "Estado": "BUILD", "Región": "ap-southeast-3", "Zona AZ": "ap-southeast-3a", "Flavor": "d6.2xlarge.2", "vCPU": 8, "RAM (GB)": 16, "IP privada": "192.168.0.30", "IP pública": "-", "Security Group": "worker-sg", "Sistema operativo": "image-centos-7", "Fecha de creación": "2026-03-10T11:45:00Z",
                 "_detalle": {"ECS ID": "mock-ecs-0004", "Tenant ID": "mock-tenant", "User ID": "mock-user", "Enterprise Project ID": "mock-ep", "Imagen": "image-centos-7", "Flavor": "d6.2xlarge.2", "Redes": "vpc-01", "Todas las IP": "192.168.0.30", "MAC address": "fa:16:3e:aa:bb:cf", "Security Groups": "worker-sg", "Volume IDs": "vol-0004", "Tags": "-", "Metadata": "{}", "Fecha de creación": "2026-03-10T11:45:00Z", "Fecha de actualización": "2026-03-10T11:45:00Z"}},
                {"Nombre ECS": "legacy-app", "Estado": "ERROR", "Región": "ap-southeast-3", "Zona AZ": "ap-southeast-3c", "Flavor": "s6.large.2", "vCPU": 2, "RAM (GB)": 4, "IP privada": "10.0.0.9", "IP pública": "-", "Security Group": "app-sg", "Sistema operativo": "image-centos-6", "Fecha de creación": "2025-06-01T00:00:00Z",
                 "_detalle": {"ECS ID": "mock-ecs-0005", "Tenant ID": "mock-tenant", "User ID": "mock-user", "Enterprise Project ID": "mock-ep", "Imagen": "image-centos-6", "Flavor": "s6.large.2", "Redes": "vpc-01", "Todas las IP": "10.0.0.9", "MAC address": "fa:16:3e:aa:bb:d0", "Security Groups": "app-sg", "Volume IDs": "vol-0005", "Tags": "-", "Metadata": "{}", "Fecha de creación": "2025-06-01T00:00:00Z", "Fecha de actualización": "2026-02-18T22:00:00Z"}},
            ],
        }
    ],
}
