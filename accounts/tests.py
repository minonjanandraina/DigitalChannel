from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SignupFlowTests(TestCase):
	def test_registration_sends_confirmation_and_activates_backoffice(self):
		response = self.client.post(reverse('accounts:register'), {
			'username': 'backoffice1',
			'email': 'backoffice@example.com',
			'password1': 'Long-local-pass-123!',
			'password2': 'Long-local-pass-123!',
		})

		self.assertRedirects(response, reverse('accounts:activation_pending'))
		user = get_user_model().objects.get(username='backoffice1')
		self.assertFalse(user.is_active)
		self.assertFalse(user.groups.exists())
		self.assertEqual(len(mail.outbox), 1)
		self.assertEqual(mail.outbox[0].to, ['backoffice@example.com'])

		activation_url = next(
			line for line in mail.outbox[0].body.splitlines()
			if line.startswith('http://')
		)
		parsed_url = urlsplit(activation_url)
		response = self.client.get(f'{parsed_url.path}?{parsed_url.query}')

		self.assertTemplateUsed(response, 'accounts/activation_result.html')
		user.refresh_from_db()
		self.assertTrue(user.is_active)
		self.assertEqual(list(user.groups.values_list('name', flat=True)), ['Backoffice'])
		self.assertFalse(user.is_staff)

	@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
	def test_failed_email_leaves_account_inactive_and_can_be_resent(self):
		from unittest.mock import patch

		with patch('accounts.views.send_activation_email', return_value=False):
			response = self.client.post(reverse('accounts:register'), {
				'username': 'waiting',
				'email': 'waiting@example.com',
				'password1': 'Long-local-pass-123!',
				'password2': 'Long-local-pass-123!',
			})

		user = get_user_model().objects.get(username='waiting')
		self.assertFalse(user.is_active)
		self.assertRedirects(
			response,
			reverse('accounts:activation_pending'),
			fetch_redirect_response=False,
		)
		pending_response = self.client.get(reverse('accounts:activation_pending'))
		self.assertContains(pending_response, 'pas pu')

		resend_response = self.client.post(reverse('accounts:resend_activation'), {
			'email': user.email,
		})
		self.assertRedirects(resend_response, reverse('accounts:resend_activation'))
		self.assertEqual(len(mail.outbox), 1)

	def test_expired_or_tampered_link_does_not_activate_account(self):
		user = get_user_model().objects.create_user(
			username='unconfirmed',
			email='unconfirmed@example.com',
			password='Long-local-pass-123!',
			is_active=False,
		)

		response = self.client.get(reverse('accounts:activate'), {'token': 'invalid-token'})

		self.assertContains(response, 'invalide ou a expiré', status_code=200)
		user.refresh_from_db()
		self.assertFalse(user.is_active)

	@override_settings(DEFAULT_SIGNUP_ROLE='')
	def test_confirmation_without_default_role_activates_without_permissions(self):
		self.client.post(reverse('accounts:register'), {
			'username': 'no-role',
			'email': 'no-role@example.com',
			'password1': 'Long-local-pass-123!',
			'password2': 'Long-local-pass-123!',
		})
		user = get_user_model().objects.get(username='no-role')
		activation_url = next(
			line for line in mail.outbox[0].body.splitlines()
			if line.startswith('http://')
		)
		parsed_url = urlsplit(activation_url)

		response = self.client.get(f'{parsed_url.path}?{parsed_url.query}')

		self.assertContains(response, 'administrateur doit attribuer un rôle')
		user.refresh_from_db()
		self.assertTrue(user.is_active)
		self.assertFalse(user.groups.exists())

	def test_inactive_account_cannot_log_in_or_open_protected_home(self):
		get_user_model().objects.create_user(
			username='inactive',
			email='inactive@example.com',
			password='Long-local-pass-123!',
			is_active=False,
		)

		login_response = self.client.post(reverse('accounts:login'), {
			'username': 'inactive',
			'password': 'Long-local-pass-123!',
		})
		home_response = self.client.get(reverse('accounts:home'))

		self.assertEqual(login_response.status_code, 200)
		self.assertRedirects(home_response, f"{reverse('accounts:login')}?next=/")

	def test_resend_email_failure_is_reported(self):
		from unittest.mock import patch

		get_user_model().objects.create_user(
			username='resend-failure',
			email='resend-failure@example.com',
			password='Long-local-pass-123!',
			is_active=False,
		)
		with patch('accounts.views.send_activation_email', return_value=False):
			response = self.client.post(reverse('accounts:resend_activation'), {
				'email': 'resend-failure@example.com',
			})

		self.assertRedirects(
			response,
			reverse('accounts:resend_activation'),
			fetch_redirect_response=False,
		)
		page = self.client.get(reverse('accounts:resend_activation'))
		self.assertContains(page, "n&#x27;a pas pu être envoyé")


class RolePermissionTests(TestCase):
	def test_initial_roles_have_expected_capabilities(self):
		viewer = Group.objects.get(name='Viewer')
		backoffice = Group.objects.get(name='Backoffice')
		admin = Group.objects.get(name='Admin')

		self.assertTrue(viewer.permissions.filter(codename='view_reconciliation_results').exists())
		self.assertFalse(viewer.permissions.filter(codename='run_mvola_reconciliation').exists())
		self.assertTrue(backoffice.permissions.filter(codename='import_mvola_report').exists())
		self.assertTrue(backoffice.permissions.filter(codename='run_mvola_reconciliation').exists())
		self.assertFalse(backoffice.permissions.filter(codename='manage_mvola_users').exists())
		self.assertTrue(admin.permissions.filter(codename='manage_mvola_users').exists())
		self.assertTrue(admin.permissions.filter(codename='delete_user').exists())

	def test_admin_group_grants_and_removes_django_staff_access(self):
		user = get_user_model().objects.create_user(
			username='role-admin',
			email='role-admin@example.com',
			password='Long-local-pass-123!',
			is_active=True,
		)
		admin_group = Group.objects.get(name='Admin')

		user.groups.add(admin_group)
		user.refresh_from_db()
		self.assertTrue(user.is_staff)

		user.groups.remove(admin_group)
		user.refresh_from_db()
		self.assertFalse(user.is_staff)


class SidebarNavigationTests(TestCase):
	def login_with_role(self, role_name):
		user = get_user_model().objects.create_user(
			username=f'sidebar-{role_name.lower()}',
			email=f'sidebar-{role_name.lower()}@example.com',
			password='Long-local-pass-123!',
			is_active=True,
		)
		user.groups.add(Group.objects.get(name=role_name))
		self.client.force_login(user)

	def test_backoffice_sees_home_then_import_without_admin_menu(self):
		self.login_with_role('Backoffice')

		response = self.client.get(reverse('accounts:home'))
		content = response.content.decode()

		self.assertLess(content.index('>Accueil</a>'), content.index('>Import MVOLA</a>'))
		self.assertNotIn('Administration Django', content)
		self.assertContains(response, 'mobileSidebar')

	def test_viewer_sees_home_but_no_import_or_admin_menu(self):
		self.login_with_role('Viewer')

		response = self.client.get(reverse('accounts:home'))

		self.assertContains(response, '>Accueil</a>')
		self.assertNotContains(response, '>Import MVOLA</a>')
		self.assertNotContains(response, 'Administration Django')

	def test_admin_menu_comes_after_mvola_import(self):
		self.login_with_role('Admin')

		response = self.client.get(reverse('accounts:home'))
		content = response.content.decode()

		self.assertLess(content.index('>Import MVOLA</a>'), content.index('>Administration Django</a>'))

	def test_backoffice_sees_om_import_and_mvola_import(self):
		self.login_with_role('Backoffice')

		response = self.client.get(reverse('accounts:home'))
		content = response.content.decode()

		self.assertIn('>Import Orange Money</a>', content)
		self.assertIn('WTB Orange Money', content)
		self.assertIn('WTB MVOLA', content)
		self.assertLess(content.index('WTB MVOLA'), content.index('WTB Orange Money'))

	def test_viewer_sees_om_history_but_no_om_import(self):
		self.login_with_role('Viewer')

		response = self.client.get(reverse('accounts:home'))

		self.assertContains(response, 'WTB Orange Money')
		self.assertNotContains(response, '>Import Orange Money</a>')

	def test_user_without_any_capability_sees_no_reconciliation_menus(self):
		user = get_user_model().objects.create_user(
			username='sidebar-no-role',
			email='sidebar-no-role@example.com',
			password='Long-local-pass-123!',
			is_active=True,
		)
		self.client.force_login(user)

		response = self.client.get(reverse('accounts:home'))

		self.assertNotContains(response, 'WTB MVOLA')
		self.assertNotContains(response, 'WTB Orange Money')
		self.assertNotContains(response, '>Rapprochement</div>')
