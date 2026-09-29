from django.urls import path
from . import views
from . import views_motions

urlpatterns = [
    path('motions/', views_motions.motions_page, name='motions'),
    path('motions/<slug:slug>/', views_motions.motions_page, name='motion'),
    path('api/motions/', views_motions.api_motions, name='api_motions'),
    path('api/motions/<slug:slug>/turns/', views_motions.api_motion_turns, name='api_motion_turns'),
    path('api/motions/<slug:slug>/sessions/', views_motions.api_motion_sessions, name='api_motion_sessions'),
    path('api/wikilinks/', views_motions.api_wikilinks, name='api_wikilinks'),
    path('api/mentions/<slug:name>/', views_motions.api_mentions, name='api_mentions'),
    path('memory_lane/', views.memory_lane, name='memory_lane'),
    path('spy/', views.stream, name='spy'),
    path('api/recent_messages/', views.recent_messages, name='recent_messages'),
    path('api/messages/', views.api_messages, name='api_messages'),
    path('api/all_messages/', views.all_messages, name='all_messages'),
    path('api/heap_metadata/', views.heap_metadata, name='heap_metadata'),
    path('api/heap_messages/<str:heap_id>/', views.heap_messages, name='heap_messages'),
    path('api/messages_since/<str:message_id>/', views.messages_since, name='messages_since'),
    path('api/ingest/', views.ingest, name='ingest'),
]
