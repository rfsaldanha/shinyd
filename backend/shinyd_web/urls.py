from django.contrib.auth import views as auth_views
from django.urls import path

from control import activation_views, agent_views, build_views, gateway_views, views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("healthz", views.health, name="health"),
    path("server/diagnostics/", agent_views.diagnostics, name="diagnostics"),
    path("connect/<uuid:pk>/", gateway_views.connect, name="gateway_connect"),
    path("login/", views.Login.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password/",
        auth_views.PasswordChangeView.as_view(template_name="control/form.html", success_url="/"),
        name="password_change",
    ),
    path("recover/<uidb64>/<token>/", views.Recover.as_view(), name="recover"),
    path("users/", views.users, name="users"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/", views.user_edit, name="user_edit"),
    path("users/<int:pk>/recovery/", views.recovery_issue, name="recovery_issue"),
    path("apps/new/", views.app_create, name="app_create"),
    path("apps/<uuid:pk>/", views.app_detail, name="app_detail"),
    path("apps/<uuid:pk>/edit/", views.app_edit, name="app_edit"),
    path("apps/<uuid:pk>/access/", views.app_access, name="app_access"),
    path("apps/<uuid:pk>/config/", views.app_config, name="app_config"),
    path("apps/<uuid:pk>/upload/", views.upload, name="upload"),
    path("jobs/<uuid:pk>/", views.job_detail, name="job_detail"),
    path("versions/<uuid:pk>/prepare/", build_views.prepare, name="version_prepare"),
    path("versions/<uuid:pk>/activate/", activation_views.activate, name="version_activate"),
    path("jobs/<uuid:pk>/cancel/", views.job_cancel, name="job_cancel"),
    path("audit/", views.audit, name="audit"),
    path("api/v1/apps/", views.api_apps, name="api_apps"),
    path("api/v1/jobs/<uuid:pk>/", views.api_job, name="api_job"),
]
