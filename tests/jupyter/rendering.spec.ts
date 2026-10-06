const { jupyterTest: test, expect, openNotebook, addCodeCell } = require(process.env.PRET_TESTING_HELPER);
const panel = ".jp-NotebookPanel:not(.lm-mod-hidden)";

const todoSource = [
  "from pret import create_store",
  "from todo_app import TodoApp",
  "state = create_store({'faire à manger': True, 'faire la vaisselle': False}, sync=True)",
  "app = TodoApp(state=state)",
  "app",
].join("\n");

test("notebook rendering sends checkbox edits to Python and receives kernel edits", async ({ page }) => {
  await openNotebook(page, "Todo.ipynb", [todoSource], { run: true });
  const checkbox = page.locator(`${panel} #faire-à-manger`);
  const count = page.locator(`${panel} .pret-view p`);
  await expect(checkbox).toBeChecked();
  await expect(count).toHaveText("Number of unfinished todo: 1");
  await checkbox.click();
  await expect(count).toHaveText("Number of unfinished todos: 2");
  await addCodeCell(page, panel, "print(state['faire à manger'])");
  await page.notebook.runCell(1, true);
  await expect(page.locator(`${panel} .jp-Cell:nth-child(2) .jp-OutputArea-output pre`)).toHaveText("False\n");
  await addCodeCell(page, panel, "state['faire à manger'] = True");
  await page.notebook.runCell(2, true);
  await expect(count).toHaveText("Number of unfinished todo: 1");
});

test("separate notebook outputs share the same Store", async ({ page }) => {
  await openNotebook(page, "Components.ipynb", [
    "from pret import create_store\nfrom todo_app import TodoList\n" +
      "state = create_store({'todos': [{'text': 'My first todo', 'done': True}, " +
      "{'text': 'My second todo', 'done': False}]})\nTodoList(state=state)",
    `from pret import component, use_store_snapshot
from pret.react import p

@component
def RemainingTodoCounter(state):
    """
    Display the count from the Store shared with the earlier notebook output
    """
    todos = use_store_snapshot(state['todos'])
    remaining = sum(not todo['done'] for todo in todos)
    return p(f'Number of unfinished todos: {remaining}.', id='remaining-todo-counter')

RemainingTodoCounter(state=state)`,
  ]);
  // Define the counter after the first output loads so the manager appends its component code
  await page.notebook.runCell(0, true);
  const checkbox = page.getByRole("checkbox", { name: "My second todo" });
  await expect(checkbox).not.toBeChecked();
  await page.notebook.runCell(1, true);
  await expect(page.locator("#remaining-todo-counter")).toHaveText("Number of unfinished todos: 1.");
  await checkbox.click();
  await expect(page.locator("#remaining-todo-counter")).toHaveText("Number of unfinished todos: 0.");
});
