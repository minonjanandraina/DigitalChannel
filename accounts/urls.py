from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from .forms import LoginForm

app_name = 'accounts'

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', auth_views.LoginView.as_view(
        template_name='accounts/login.html',
        authentication_form=LoginForm,
        redirect_authenticated_user=True,
    ), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('register/', views.register, name='register'),
    path('activate/', views.activate_email, name='activate'),
    path('activation-pending/', views.activation_pending, name='activation_pending'),
    path('resend-activation/', views.resend_activation, name='resend_activation'),
]