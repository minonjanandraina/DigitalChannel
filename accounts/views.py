import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired
from django.db import transaction
from django.shortcuts import redirect, render

from .emails import send_activation_email
from .forms import ResendActivationForm, SignupForm

logger = logging.getLogger(__name__)
ACTIVATION_SALT = 'accounts.email-activation'


@login_required
def home(request):
	return render(request, 'accounts/home.html', {
		'role_names': request.user.groups.values_list('name', flat=True),
	})


def register(request):
	if request.user.is_authenticated:
		return redirect('accounts:home')

	form = SignupForm(request.POST or None)
	if request.method == 'POST' and form.is_valid():
		user = form.save(commit=False)
		user.is_active = False
		user.save()
		if send_activation_email(request, user, ACTIVATION_SALT):
			messages.success(
				request,
				'Un lien de confirmation vient de vous être envoyé par email.',
			)
		else:
			messages.error(
				request,
				"L'email n'a pas pu être envoyé. Votre compte reste en attente; "
				'vous pourrez demander un nouvel envoi.',
			)
		return redirect('accounts:activation_pending')

	return render(request, 'accounts/register.html', {'form': form})


def activation_pending(request):
	return render(request, 'accounts/activation_pending.html')


def resend_activation(request):
	form = ResendActivationForm(request.POST or None)
	if request.method == 'POST' and form.is_valid():
		User = get_user_model()
		user = User.objects.filter(
			email__iexact=form.cleaned_data['email'],
			is_active=False,
		).first()
		delivery_failed = bool(
			user and not send_activation_email(request, user, ACTIVATION_SALT)
		)
		if delivery_failed:
			messages.error(request, "L'email n'a pas pu être envoyé. Réessayez plus tard.")
		else:
			messages.success(
				request,
				'Si un compte en attente correspond à cette adresse, '
				'un nouveau lien sera envoyé.',
			)
		return redirect('accounts:resend_activation')

	return render(request, 'accounts/resend_activation.html', {'form': form})


def activate_email(request):
	token = request.GET.get('token', '')
	try:
		payload = signing.loads(
			token,
			salt=ACTIVATION_SALT,
			max_age=settings.EMAIL_CONFIRMATION_MAX_AGE,
		)
	except (BadSignature, SignatureExpired, TypeError):
		return render(request, 'accounts/activation_result.html', {
			'activated': False,
			'message': 'Ce lien est invalide ou a expiré. Demandez un nouveau lien.',
		})

	User = get_user_model()
	user = User.objects.filter(
		pk=payload.get('user_id'),
		email=payload.get('email'),
	).first()
	if user is None:
		return render(request, 'accounts/activation_result.html', {
			'activated': False,
			'message': 'Ce lien ne correspond à aucun compte.',
		})
	if user.is_active:
		return render(request, 'accounts/activation_result.html', {
			'activated': True,
			'message': 'Cette adresse email est déjà confirmée.',
		})

	role_name = settings.DEFAULT_SIGNUP_ROLE
	group = Group.objects.filter(name=role_name).first() if role_name else None
	if role_name and group is None:
		logger.error('Default signup role %r does not exist.', settings.DEFAULT_SIGNUP_ROLE)
		return render(request, 'accounts/activation_result.html', {
			'activated': False,
			'message': "Le rôle initial n'est pas configuré. Contactez l'administrateur.",
		})

	with transaction.atomic():
		user.is_active = True
		user.save(update_fields=['is_active'])
		if group:
			user.groups.add(group)

	return render(request, 'accounts/activation_result.html', {
		'activated': True,
		'message': (
			'Votre email est confirmé. Vous pouvez maintenant vous connecter.'
			if group else
			"Votre email est confirmé. Un administrateur doit attribuer un rôle "
			'avant l’accès aux fonctions métier.'
		),
	})

