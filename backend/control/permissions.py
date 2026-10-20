from django.db.models import Q

from .models import AppGrant, Application


def is_admin(user):
    return user.is_authenticated and user.is_active and user.is_superuser


def can_create(user):
    return user.is_authenticated and user.is_active and (user.is_superuser or user.can_publish)


def manageable_apps(user):
    if not user.is_authenticated or not user.is_active:
        return Application.objects.none()
    if is_admin(user):
        return Application.objects.all()
    return Application.objects.filter(
        Q(owner=user)
        | Q(grants__role=AppGrant.Role.PUBLISHER, grants__user=user)
        | Q(grants__role=AppGrant.Role.PUBLISHER, grants__group__in=user.groups.all())
    ).distinct()


def visible_apps(user):
    public = Q(visibility=Application.Visibility.PUBLIC)
    if not user.is_authenticated or not user.is_active:
        return Application.objects.filter(public)
    if is_admin(user):
        return Application.objects.all()
    return Application.objects.filter(
        public | Q(owner=user) | Q(grants__user=user) | Q(grants__group__in=user.groups.all())
    ).distinct()
