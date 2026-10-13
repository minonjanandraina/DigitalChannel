from django import forms

from .repository import MODULES, REGULARIZATION_CHOICES


class ReportFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False, label='Du',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    date_to = forms.DateField(
        required=False, label='Au',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    process_id = forms.IntegerField(
        required=False, min_value=1, label='Processus',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
    )
    status = forms.ChoiceField(required=False, label='Statut', widget=forms.Select(attrs={'class': 'form-select'}))
    regularization = forms.ChoiceField(
        required=False, label='Régularisation',
        choices=(('', 'Toutes'),) + REGULARIZATION_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    transaction_id = forms.CharField(
        required=False, max_length=100, label='Transaction ID',
        widget=forms.TextInput(attrs={'class': 'form-control', 'maxlength': '100'}),
    )
    phone = forms.CharField(
        required=False, max_length=32, label='Téléphone',
        widget=forms.TextInput(attrs={'class': 'form-control', 'maxlength': '32'}),
    )
    amount = forms.DecimalField(
        required=False, max_digits=24, decimal_places=4, label='Montant exact',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.0001'}),
    )
    amount_min = forms.DecimalField(
        required=False, max_digits=24, decimal_places=4, label='Montant min',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.0001'}),
    )
    amount_max = forms.DecimalField(
        required=False, max_digits=24, decimal_places=4, label='Montant max',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.0001'}),
    )

    def __init__(self, *args, module, **kwargs):
        super().__init__(*args, **kwargs)
        config = MODULES[module]
        self.fields['status'].choices = (('', 'Tous les statuts'),) + config['statuses']
        self.fields['phone'].label = config['phone_label']

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('date_from') and cleaned.get('date_to') and cleaned['date_from'] > cleaned['date_to']:
            self.add_error('date_to', 'La date de fin doit être postérieure ou égale à la date de début.')
        if (
            cleaned.get('amount_min') is not None and cleaned.get('amount_max') is not None
            and cleaned['amount_min'] > cleaned['amount_max']
        ):
            self.add_error('amount_max', 'Le montant max doit être supérieur ou égal au montant min.')
        return cleaned
