from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = "Nom d'utilisateur"
        self.fields['username'].widget.attrs.update({
            'class': 'form-control',
            'autocomplete': 'username',
            'autofocus': True,
        })
        self.fields['password'].label = 'Mot de passe'
        self.fields['password'].widget.attrs.update({
            'class': 'form-control',
            'autocomplete': 'current-password',
        })


class SignupForm(UserCreationForm):
    email = forms.EmailField(label='Adresse email')

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('username', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')
        self.fields['username'].label = "Nom d'utilisateur"
        self.fields['password1'].label = 'Mot de passe'
        self.fields['password2'].label = 'Confirmer le mot de passe'

    def clean_email(self):
        email = self.cleaned_data['email'].strip()
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('Un compte utilise déjà cette adresse email.')
        return email


class ResendActivationForm(forms.Form):
    email = forms.EmailField(label='Adresse email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].widget.attrs.update({
            'class': 'form-control',
            'autocomplete': 'email',
        })