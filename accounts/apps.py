from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self):
        from django.db.models.signals import post_migrate

        from . import signals

        post_migrate.connect(
            signals.create_default_roles,
            sender=self,
            dispatch_uid='accounts.create_default_roles',
        )
