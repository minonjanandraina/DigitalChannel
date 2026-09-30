from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType


CAPABILITY_NAMES = {
    'view_mvola_transactions': 'Can view MVOLA transactions',
    'view_reconciliation_results': 'Can view reconciliation results',
    'import_mvola_report': 'Can import MVOLA reports',
    'run_mvola_reconciliation': 'Can run MVOLA reconciliation',
    'import_om_report': 'Can import Orange Money reports',
    'run_om_reconciliation': 'Can run Orange Money reconciliation',
    'manage_mvola_users': 'Can manage DigitalChannel users and roles',
}

ROLE_CAPABILITIES = {
    'Viewer': {
        'view_mvola_transactions',
        'view_reconciliation_results',
    },
    'Backoffice': {
        'view_mvola_transactions',
        'view_reconciliation_results',
        'import_mvola_report',
        'run_mvola_reconciliation',
        'import_om_report',
        'run_om_reconciliation',
    },
    'Admin': set(CAPABILITY_NAMES),
}


def seed_roles(using='default'):
    User = get_user_model()
    content_type = ContentType.objects.db_manager(using).get_for_model(User)
    capabilities = {}
    for codename, name in CAPABILITY_NAMES.items():
        permission, _ = Permission.objects.using(using).get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={'name': name},
        )
        capabilities[codename] = permission

    admin_auth_permissions = Permission.objects.using(using).filter(
        content_type__app_label='auth',
        codename__in={
            'view_user', 'add_user', 'change_user', 'delete_user',
            'view_group', 'add_group', 'change_group', 'delete_group',
        },
    )
    for role_name, codenames in ROLE_CAPABILITIES.items():
        group, _ = Group.objects.using(using).get_or_create(name=role_name)
        permissions = [capabilities[codename] for codename in codenames]
        if role_name == 'Admin':
            permissions.extend(admin_auth_permissions)
        group.permissions.set(permissions)