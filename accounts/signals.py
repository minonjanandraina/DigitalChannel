from django.contrib.auth import get_user_model
from django.db.models.signals import m2m_changed

from .roles import seed_roles

User = get_user_model()


def create_default_roles(sender, using, **kwargs):
    seed_roles(using=using)


def sync_staff_access(sender, instance, action, **kwargs):
    if not isinstance(instance, User) or action not in {
        'post_add', 'post_remove', 'post_clear',
    }:
        return

    should_be_staff = instance.is_superuser or instance.groups.filter(name='Admin').exists()
    if instance.is_staff != should_be_staff:
        User.objects.filter(pk=instance.pk).update(is_staff=should_be_staff)
        instance.is_staff = should_be_staff


m2m_changed.connect(
    sync_staff_access,
    sender=User.groups.through,
    dispatch_uid='accounts.sync_admin_group_staff_access',
)