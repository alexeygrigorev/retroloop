"""The one form the upload page renders, and every check it makes.

The form is the server-side gate: a file is refused for its extension, for
being empty and for being over the cap before a single byte reaches the scratch
volume. The page repeats the list and the cap in words, but nothing on the page
is what enforces them.

It is a plain `Form`, not a `ModelForm`. Nothing a facilitator types is a field
of `MeetingRecord` — the kind is derived from the extension, the size is
counted while writing, and the path is generated — so a model form would be a
form with every field excluded.
"""

from django import forms

from meetings.models import MeetingRecord
from meetings.uploads import (
    ALLOWED_EXTENSIONS,
    AUDIO_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_LABEL,
    TRANSCRIPT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    extension_of,
    is_allowed,
)


def _joined(extensions: tuple[str, ...]) -> str:
    return ", ".join(extensions)


class MeetingUploadForm(forms.Form):
    """A recording, a transcript file, or the transcript pasted in — one of them."""

    media = forms.FileField(
        required=False,
        label="A recording or a transcript file",
        help_text=(
            f"Audio ({_joined(AUDIO_EXTENSIONS)}), "
            f"video ({_joined(VIDEO_EXTENSIONS)}) "
            f"or a transcript ({_joined(TRANSCRIPT_EXTENSIONS)}). "
            f"Up to {MAX_UPLOAD_LABEL}."
        ),
    )
    pasted_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 10}),
        label="Or paste the transcript",
        help_text="Already have the text? Paste it here and nothing has to be transcribed.",
    )

    def clean_media(self):
        upload = self.cleaned_data.get("media")
        if upload is None:
            return None

        if not is_allowed(upload.name):
            extension = extension_of(upload.name) or "no extension"
            raise forms.ValidationError(
                f"{extension} files are not accepted. Upload one of: {_joined(ALLOWED_EXTENSIONS)}."
            )
        if upload.size == 0:
            raise forms.ValidationError("That file is empty, so there is nothing to process.")
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError(
                f"That file is larger than the {MAX_UPLOAD_LABEL} limit. "
                "Upload the audio on its own, or paste the transcript instead."
            )
        return upload

    def clean(self):
        cleaned = super().clean()
        upload = cleaned.get("media")
        # CharField strips by default, so a box holding only spaces arrives
        # here as an empty string and is refused with everything else that is
        # empty.
        text = cleaned.get("pasted_text") or ""

        if upload and text:
            raise forms.ValidationError(
                "Send one thing at a time: either the file or the pasted transcript."
            )
        # Nothing at all, an empty box, and a file that failed its own check
        # all land here; the field error explains the last of them already.
        if not upload and not text and not self.errors:
            raise forms.ValidationError(
                "There is nothing to process. Upload a file, or paste the transcript text."
            )
        return cleaned

    def kind(self) -> str:
        """Which sort of record this submission makes, from the extension.

        Only meaningful once the form has validated, which is the only place it
        is called from.
        """
        upload = self.cleaned_data.get("media")
        if upload is None:
            return MeetingRecord.Kind.PASTED_TEXT

        extension = extension_of(upload.name)
        if extension in AUDIO_EXTENSIONS:
            return MeetingRecord.Kind.AUDIO
        if extension in VIDEO_EXTENSIONS:
            return MeetingRecord.Kind.VIDEO
        return MeetingRecord.Kind.TRANSCRIPT_FILE
