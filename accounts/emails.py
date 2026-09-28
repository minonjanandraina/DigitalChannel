import logging

from django.conf import settings
from django.core.mail import send_mail
from django.core import signing
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import urlencode

logger = logging.getLogger(__name__)


def send_activation_email(request, user, salt):
    token = signing.dumps({'user_id': user.pk, 'email': user.email}, salt=salt)
    activation_url = request.build_absolute_uri(reverse('accounts:activate'))
    activation_url = f'{activation_url}?{urlencode({"token": token})}'
    context = {'user': user, 'activation_url': activation_url}
    text_body = render_to_string('accounts/email/activation.txt', context)
    html_body = render_to_string('accounts/email/activation.html', context)

    try:
        return send_mail(
            subject='Confirmez votre adresse email',
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_body,
            fail_silently=False,
        ) == 1
    except Exception:
        logger.exception('Could not send account activation email to user %s.', user.pk)
        return False