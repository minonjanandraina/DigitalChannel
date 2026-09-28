import csv
import io
import re
from datetime import datetime

from django import forms
from django.conf import settings


EXPECTED_COLUMNS = (
    'DATE_TRANS',
    'TRANSID_MVOLA',
    'STATE',
    'MSISDN',
    'PIVOT',
    'SENS',
    'NOM',
    'TRANSID_PARENT',
    'TRANS_TYPE',
    'AMOUNT',
    'SOLDE_PIVOT_AVANT',
    'SOLDE_PIVOT_APRES',
    'ORIGFTID',
    'TYPE_OPERATION',
)
FILENAME_PATTERN = re.compile(r'\A\d{4}-\d{2}-\d{2}_reporting_PAMF\.csv\Z')


class MvolaReportUploadForm(forms.Form):
    report = forms.FileField(label='Rapport MVOLA au format CSV')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['report'].widget.attrs.update({
            'class': 'form-control',
            'accept': '.csv,text/csv',
        })
        self.row_count = 0

    def clean_report(self):
        uploaded = self.cleaned_data['report']
        filename = uploaded.name or ''
        if '/' in filename or '\\' in filename or not FILENAME_PATTERN.fullmatch(filename):
            raise forms.ValidationError(
                'Le nom doit respecter le format YYYY-MM-DD_reporting_PAMF.csv, sans chemin.'
            )
        try:
            datetime.strptime(filename[:10], '%Y-%m-%d')
        except ValueError as exc:
            raise forms.ValidationError('La date du nom de fichier est invalide.') from exc

        if uploaded.size == 0:
            raise forms.ValidationError('Le fichier est vide.')
        if uploaded.size > settings.MVOLA_MAX_UPLOAD_SIZE:
            max_mib = settings.MVOLA_MAX_UPLOAD_SIZE // (1024 * 1024)
            raise forms.ValidationError(f'Le fichier dépasse la limite de {max_mib} Mio.')

        try:
            content = uploaded.read()
            text = content.decode('utf-8-sig')
            if '\x00' in text:
                raise forms.ValidationError('Le fichier contient des données invalides.')
            reader = csv.reader(io.StringIO(text, newline=''), delimiter=';', strict=True)
            headers = next(reader, None)
            if headers != list(EXPECTED_COLUMNS):
                raise forms.ValidationError(
                    'Les colonnes du CSV ne correspondent pas au format de rapport MVOLA attendu.'
                )

            row_count = 0
            completed_count = 0
            for row in reader:
                if not row or all(not value.strip() for value in row):
                    continue
                if len(row) != len(EXPECTED_COLUMNS):
                    raise forms.ValidationError(
                        f'Ligne CSV {reader.line_num}: nombre de colonnes incorrect.'
                    )
                row_count += 1
                if row[2] == 'Completed':
                    if not row[1].strip():
                        raise forms.ValidationError(
                            f'Ligne CSV {reader.line_num}: TRANSID_MVOLA est obligatoire pour une transaction Completed.'
                        )
                    completed_count += 1
        except UnicodeDecodeError as exc:
            raise forms.ValidationError('Le CSV doit être encodé en UTF-8.') from exc
        except csv.Error as exc:
            raise forms.ValidationError(f'CSV illisible: {exc}') from exc

        if row_count == 0:
            raise forms.ValidationError('Le rapport ne contient aucune ligne de données.')
        if completed_count == 0:
            raise forms.ValidationError('Le rapport ne contient aucune transaction Completed.')

        uploaded.seek(0)
        self.row_count = row_count
        self.completed_count = completed_count
        self.content = content
        return uploaded


RECONCILIATION_STATUS_CHOICES = (
    ('', 'Tous les statuts'),
    ('matched', 'Rapprochées'),
    ('orphan_mvola', 'Orphelines MVOLA'),
    ('orphan_pamf', 'Orphelines PAMF'),
)

RESULT_SOURCE_CHOICES = (
    ('reconciliation', 'Écarts et rapprochement'),
    ('mvola', 'Transactions MVOLA'),
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
    phone = forms.CharField(
        required=False,
        max_length=32,
        label='Téléphone',
        widget=forms.TextInput(attrs={'class': 'form-control', 'maxlength': '32'}),
    )
    amount = forms.DecimalField(
        required=False,
        max_digits=24,
        decimal_places=4,
        label='Montant (MVOLA ou PAMF)',
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.0001'}),
    )