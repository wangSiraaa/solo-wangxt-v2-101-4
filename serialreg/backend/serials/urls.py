from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BindingViewSet, IssueNumberViewSet, IssueViewSet, ItemViewSet,
    TimelineViewSet, TitleSuccessionViewSet, TitleViewSet,
)

router = DefaultRouter()
router.register("titles", TitleViewSet, basename="title")
router.register("numbers", IssueNumberViewSet)
router.register("issues", IssueViewSet)
router.register("items", ItemViewSet, basename="item")
router.register("bindings", BindingViewSet)
router.register("successions", TitleSuccessionViewSet, basename="succession")
router.register("timeline", TimelineViewSet, basename="timeline")

urlpatterns = [
    path("", include(router.urls)),
]
