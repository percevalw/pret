"""
Render a component whose async effect reads bytes from a server function, for the rendering tests
"""

from pret import component, server_only, use_effect, use_state
from pret.react import button, div, span


@server_only
def kernel_bytes(count):
    """
    Return raw bytes, received by the browser as a Uint8Array
    """
    return bytes([count, 255])


@component
def KernelBytes():
    """
    Show the bytes read by an async effect, rerun each time the button changes its count
    """
    count, set_count = use_state(1)
    received, set_received = use_state("")

    async def read_bytes():
        # An async effect, rerun when the count changes, must not leave its Promise as React cleanup
        data = await kernel_bytes(count)
        set_received(" ".join([str(data.length), "bytes", str(data[0]), str(data[1])]))

    use_effect(read_bytes, [count])
    return div(
        button("Next", on_click=lambda event: set_count(count + 1), id="next-count"),
        span(received, id="kernel-bytes"),
    )
