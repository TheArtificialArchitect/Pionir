"""Social posting: the shared vocabulary between the crew that drafts a post and the Pionir
adapter that publishes it. One author, one module, so the two sides cannot drift apart.

``card`` draws the image from the post's text; ``post`` checks the text. What the owner
approves is the text, and the image is a pure function of it (same renderer, same font), so
what he sees on the approval card is byte for byte what gets posted.
"""
