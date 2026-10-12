from django.urls import path

from . import views

app_name = 'replay'

urlpatterns = [
    path('<str:source>/<int:reconciliation_id>/', views.replay_panel, name='panel'),
    path('<str:source>/<int:reconciliation_id>/maker/', views.replay_maker, name='maker'),
    path('<str:source>/<int:reconciliation_id>/checker/', views.replay_checker, name='checker'),
]
