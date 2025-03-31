from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),        # GET: render chat interface
    path("upload", views.upload, name="upload"),# POST: handle PDF uploads
    path("ask", views.ask, name="ask"),         # POST: handle question submissions
    path("files", views.files_view, name="files_view"),
    path("remove_file", views.remove_file, name="remove_file"),
]