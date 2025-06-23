# TicTacToe

Let's build a simple TicTacToe game using Pret. We will build a simple game where two players can play against each other.

```{ .python .render-with-pret }
from pret import create_store, component
from pret.ui.react import button, div
from pret.hooks import use_store_snapshot

state = create_store({
    "board": [0] * 9,
    "turn": 1,
    "winning_pattern": [],
})

winning_patterns = [
    [0, 1, 2],
    [3, 4, 5],
    [6, 7, 8],
    [0, 3, 6],
    [1, 4, 7],
    [2, 5, 8],
    [0, 4, 8],
    [6, 4, 2],
]

SQUARE_STYLE = {
    "display": "flex",
    "justifyContent": "center",
    "alignItems": "center",
    "width": "60px",
    "height": "60px",
    "margin": "2px",
    "fontSize": "24px",
    "padding": "0",
    "appearance": "none",
    "border": "1px solid grey",
}

WINNING_SQUARE_STYLE = {
    **SQUARE_STYLE,
    "background": "lightGreen",
}

GRID_STYLE = {
    "display": "grid",
    "gridTemplateColumns": "repeat(3, 66px)",
    "gridGap": "2px",
}


@component
def TicTacToe():
    tracked = use_store_snapshot(state)
    winning_pattern = tracked["winning_pattern"]

    def on_click_square(idx):
        # if the game is over, clean everything on click
        if bool(state["winning_pattern"]) or 0 not in state["board"]:
            state["board"][:] = [0 for _ in range(9)]
            state["winning_pattern"] = []
            return

        # place a piece on the board
        value = state["board"][idx]
        if value == 0:
            state["board"][idx] = state["turn"]
        state["turn"] = 2 if state["turn"] == 1 else 1

        # check for victory
        for pattern in winning_patterns:
            players = set(state["board"][i] for i in pattern)
            if len(players) == 1 and 0 not in players:
                state["winning_pattern"] = pattern

    return div(
        [
            button(
                "X" if square == 2 else "O" if square == 1 else "",
                on_click=lambda event, idx=idx: on_click_square(idx),
                style=(
                    WINNING_SQUARE_STYLE if idx in winning_pattern else SQUARE_STYLE
                ),
            )
            for idx, square in enumerate(tracked["board"])
        ],
        style=GRID_STYLE,
    )


TicTacToe()
```
