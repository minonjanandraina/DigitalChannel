import io
import re
from datetime import datetime

import pandas as pd
from django import forms
from django.conf import settings

FILENAME_PATTERN = re.compile(
    r'\ADaily-ChannelUserTransactionReport-0324660679-\d{8}\.xls\Z'
)

# Colonnes positionnelles attendues, dans l'ordre lu par service_om.read_om_transaction()
# (le fichier n'a pas de ligne d'en-tête exploitable à une position fixe: le rapport Orange
# Money embarque plusieurs lignes de métadonnées puis un ou plusieurs blocs de transactions,
# chacun précédé de sa propre ligne d'en-tête "Statut/..."; on garde donc le même parti pris
# que service_om.py: lecture positionnelle sur 17 colonnes, puis filtrage par la valeur de
# statut plutôt que par une ligne d'en-tête unique).
OM_COLUMNS = [
    'No', 'date', 'hour', 'trx_id', 'service', 'transaction_type', 'status', 'mode',
    'technical_account', 'wallet_technical_account', 'pseudo', 'client_account',
    'wallet_type', 'debit', 'credit', 'commission_amount', 'sous_reseau',
]


def parse_transaction_date(filename):
    """Mirrors service_om.read_om_transaction()'s filename[46:][:8] slicing."""
    return datetime.strptime(filename[46:54], '%Y%m%d').date()


class OmReportUploadForm(forms.Form):
    report = forms.FileField(label='Rapport Orange Money au format XLS')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['report'].widget.attrs.update({
            'class': 'form-control',
            'accept': '.xls',
        })
        self.row_count = 0
        self.completed_count = 0

    def clean_report(self):
        uploaded = self.cleaned_data['report']
        filename = uploaded.name or ''
        if '/' in filename or '\\' in filename or not FILENAME_PATTERN.fullmatch(filename):
            raise forms.ValidationError(
                'Le nom doit respecter le format '
                'Daily-ChannelUserTransactionReport-0324660679-YYYYMMDD.xls, sans chemin.'
            )
        try:
            parse_transaction_date(filename)
        except ValueError as exc:
            raise forms.ValidationError('La date du nom de fichier est invalide.') from exc

        if uploaded.size == 0:
            raise forms.ValidationError('Le fichier est vide.')
        if uploaded.size > settings.OM_MAX_UPLOAD_SIZE:
            max_mib = settings.OM_MAX_UPLOAD_SIZE // (1024 * 1024)
            raise forms.ValidationError(f'Le fichier dépasse la limite de {max_mib} Mio.')

        content = uploaded.read()
        try:
            dataframe = pd.read_excel(io.BytesIO(content), names=OM_COLUMNS, engine='xlrd')
        except Exception as exc:
            raise forms.ValidationError(
                "Le fichier n'est pas un rapport Orange Money .xls lisible."
            ) from exc

        status_column = dataframe['status'].astype(str).str.strip()
        row_count = int(status_column.isin(['Succès', 'Echec']).sum())
        completed_count = int((status_column == 'Succès').sum())

        if row_count == 0:
            raise forms.ValidationError('Le rapport ne contient aucune ligne de transaction reconnaissable.')
        if completed_count == 0:
            raise forms.ValidationError('Le rapport ne contient aucune transaction Succès.')

        uploaded.seek(0)
        self.row_count = row_count
        self.completed_count = completed_count
        self.content = content
        return uploaded


RECONCILIATION_STATUS_CHOICES = (
    ('', 'Tous les statuts'),
    ('matched', 'Rapprochées'),
    ('orphan_om', 'Orphelines OM'),
    ('orphan_pamf', 'Orphelines PAMF'),
)

RESULT_SOURCE_CHOICES = (
    ('reconciliation', 'Écarts et rapprochement'),
    ('om', 'Transactions Orange Money'),
    ('cbs', 'Transactions CBS'),
)


class ReconciliationHistoryFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        label='Du',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    date_to = forms.DateField(
        required=False,
        label='Au',
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
    )
    process_id = forms.IntegerField(
        required=False,
        min_value=1,
        label='Processus',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=RECONCILIATION_STATUS_CHOICES,
        label='Statut',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    def clean(self):
        cleaned_data = super().clean()
        date_from = cleaned_data.get('date_from')
        date_to = cleaned_data.get('date_to')
        if date_from and date_to and date_from > date_to:
            self.add_error('date_to', 'La date de fin doit être postérieure ou égale à la date de début.')
        return cleaned_data


class OrphanActionForm(forms.Form):
    STATUS_CHOICES = (
        ('processing', 'En cours de traitement'),
        ('done', 'Régularisée'),
    )

    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        label='Action',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    comments = forms.CharField(
        required=False,
        label='Commentaire',
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
    )


class ReconciliationDetailFilterForm(forms.Form):
    transaction_id = forms.CharField(
        required=False,
        max_length=100,
        label='Transaction ID',
        widget=forms.TextInput(attrs={'class': 'form-control', 'maxlength': '100'}),
    )
    account = forms.CharField(
        required=False,
        max_length=32,
        label='Compte / Pseudo',
        widget=forms.TextInput(attrs={'class': 'form-control', 'maxlength': '32'}),
    )
    amount = forms.DecimalField(
        required=False,
        max_digits=24,
        decimal_places=4,
        label='Montant (OM ou PAMF)',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.0001'}),
    )
