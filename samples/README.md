# Sample photos

Ten photos, five per feed, so you can try the app without your own photos or a Bluesky account.

- `<Feed>/holding-pen/`: three photos waiting to be reviewed
- `<Feed>/curated/`: two photos ready to post
- `metadata_<Feed>.json`: their alt text, place and date

All camera metadata (EXIF, GPS, XMP) has been removed from the image files, and the metadata files
have no coordinates. The photos are by the repo's author and covered by its [license](../LICENSE).

## Try it

From `app/`, after the laptop setup in the [README](../README.md#setup-laptop):

```sh
# the curation app, pointed at the samples (edits are saved to samples/metadata_*.json)
PHOTOS_ROOT=../samples METADATA_DIR=../samples .venv/bin/python3 curate.py
# then open http://127.0.0.1:5001

# what the poster would post, without posting or saving anything
PHOTOS_ROOT=../samples METADATA_DIR=../samples .venv/bin/python3 poster.py \
    --once --dry-run --workers PostcardsFromHome,SomePostcards
```

`git checkout -- samples` puts the samples back the way they were.
