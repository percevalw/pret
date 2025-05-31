import "regenerator-runtime/runtime";
import React from "react";

import {ArrayExt, filter, toArray} from '@lumino/algorithm'; /* @ts-ignore */
import {AttachedProperty} from '@lumino/properties'; /* @ts-ignore */
import {DisposableDelegate} from '@lumino/disposable'; /* @ts-ignore */
import {KernelMessage} from '@jupyterlab/services'; /* @ts-ignore */
import {IDocumentManager} from '@jupyterlab/docmanager'; /* @ts-ignore */
import {IMainMenu} from '@jupyterlab/mainmenu'; /* @ts-ignore */
import {ILoggerRegistry, LogLevel} from '@jupyterlab/logconsole'; /* @ts-ignore */
import {IRenderMimeRegistry} from '@jupyterlab/rendermime'; /* @ts-ignore */
import {MainAreaWidget, WidgetTracker,} from '@jupyterlab/apputils'; /* @ts-ignore */
import {INotebookTracker, Notebook, NotebookPanel} from '@jupyterlab/notebook'; /* @ts-ignore */
import {ISettingRegistry} from '@jupyterlab/settingregistry'; /* @ts-ignore */
import {ILayoutRestorer, JupyterFrontEnd, JupyterFrontEndPlugin} from '@jupyterlab/application'; /* @ts-ignore */
import {OutputArea} from '@jupyterlab/outputarea'; /* @ts-ignore */
import {CodeCell, ICodeCellModel} from '@jupyterlab/cells'; /* @ts-ignore */
// import {LabIcon} from '@jupyterlab/ui-components'; /* @ts-ignore */
import {Panel, Widget} from "@lumino/widgets"; /* @ts-ignore */
import {UUID} from "@lumino/coreutils"; /* @ts-ignore */
import {CommandRegistry} from "@lumino/commands"; /* @ts-ignore */

import PretJupyterHandler from "./manager";
import {PretViewWidget} from "./widget";

import "./keep-hidden-cell-output";
import "./style.css";

import '@pret-globals';

const MIMETYPE = 'application/vnd.pret+json';
// // export const notebookIcon = new LabIcon({name: 'ui-components:pret', svgstr: pretSvgstr});

export const contextToPretJupyterHandlerRegistry = new AttachedProperty({
    name: 'widgetManager',
    create: () => undefined
});
const SETTINGS = {saveState: false};

/**
 * Iterate through all pret renderers in a notebook.
 */
function* getWidgetsFromNotebook(notebook: Notebook) {
    // @ts-ignore
    for (const cell of notebook.widgets) {
        if (cell.model.type === 'code') {
            // @ts-ignore
            for (const codecell of cell.outputArea.widgets) {
                for (const output of toArray(codecell.children())) {
                    if (output instanceof PretViewWidget) {
                        yield output;
                    }
                }
            }
        }
    }
}


function* chain(...args) {
    for (const it of args) {
        yield* it;
    }
}

/**
 * Iterate through all matching linked output views
 */
function* getLinkedWidgetsFromApp(
    jupyterApp: JupyterFrontEnd,
    path: string,
) {
    const linkedViews = filter(
        Array.from(jupyterApp.shell.widgets("main")),
        // @ts-ignore
        (w: Widget) => w.id.startsWith('LinkedOutputView-') && w.path === path);
    for (const view of toArray(linkedViews)) {
        for (const outputs of view.children()) {
            for (const output of outputs.children()) {
                // TODO: do we need instanceof ?
                if (output instanceof PretViewWidget) {
                    yield output;
                }
            }
        }
    }
}

type PretClonedAreaOptions = {
    /**
     * The notebook associated with the cloned output area.
     */
    notebook: NotebookPanel;

    /**
     * The cell for which to clone the output area.
     */
    cell?: CodeCell;

    /**
     * If the cell is not available, provide the index
     * of the cell for when the notebook is loaded.
     */
    index?: number;
}
/**
 * A widget hosting a cloned output area.
 */
export class PretClonedArea extends Panel {
    constructor(options: PretClonedAreaOptions) {
        super();

        this._notebook = options.notebook;
        this._index = options.index !== undefined ? options.index : -1;
        this._cell = options.cell || null;

        this.id = `PretArea-${UUID.uuid4()}`;
        // this.title.icon = notebookIcon;
        this.title.caption = this._notebook.title.label ? `For Notebook: ${this._notebook.title.label || ''}` : '';
        this.addClass('jp-LinkedOutputView');

        // Wait for the notebook to be loaded before
        // cloning the output area.
        void this._notebook.context.ready.then(() => {
            if (!this._cell) {
                this._cell = this._notebook.content.widgets[this._index] as CodeCell;
            }
            if (!this._cell || this._cell.model.type !== 'code') {
                this.dispose();
                return;
            }
            // @ts-ignore
            // const widget = this._cell.outputArea.widgets?.[0]?.widgets?.[1] as PretWidget;
            // TODO title label
            const clone = this._cell.cloneOutputArea();
            this.addWidget(clone);
        });
    }

    /**
     * The index of the cell in the notebook.
     */
    get index(): number {
        return this._cell
            ? ArrayExt.findFirstIndex(
                this._notebook.content.widgets,
                c => c === this._cell
            )
            : this._index;
    }

    /**
     * The path of the notebook for the cloned output area.
     */
    get path(): string {
        return this._notebook.context.path;
    }

    private _notebook: NotebookPanel;
    private _index: number;
    private _cell: CodeCell | null = null;
}

/*
Here we add the singleton PretJupyterHandler to the given editor (context)
 */
export function registerPretJupyterHandler(
    context,
    rendermime: IRenderMimeRegistry,
    renderers: Generator<PretViewWidget>,
) {
    const ensureManager = () => {
        if (manager) {
            return manager;
        }
        const instance = new PretJupyterHandler(context, SETTINGS);
        // @ts-ignore
        window.pretManager = instance;
        contextToPretJupyterHandlerRegistry.set(context, instance);
        manager = instance;
        return instance;
    }
    let manager = contextToPretJupyterHandlerRegistry.get(context);

    for (const r of renderers) {
        r.manager = ensureManager();
    }

    // Replace the placeholder widget renderer with one bound to this widget
    // manager.
    rendermime.removeMimeType(MIMETYPE);
    rendermime.addFactory(
        {
            safe: true,
            mimeTypes: [MIMETYPE],
            // @ts-ignore
            createRenderer: options => new PretViewWidget(options, ensureManager())
        },
        0
    );

    return new DisposableDelegate(() => {
        if (rendermime) {
            rendermime.removeMimeType(MIMETYPE);
        }
        if (manager) {
            manager.dispose();
        }
    });
}

export function registerOutputListener(
    notebook: Notebook,
    listener
) {
    let callbacks = [];
    notebook.model.cells.changed.connect((cells, changes) => {
        changes.newValues.forEach((cell: ICodeCellModel) => {
            const signal = cell?.outputs?.changed;
            if (!signal)
                return;

            const callback = (outputArea, outputChanges) => {
                for (let index = 0; index < notebook.model.cells.length; index++) {
                    if (cell === notebook.model.cells.get(index)) {
                        const detachPret = outputChanges.newValues.some(outputModel =>
                            !!outputModel._rawData?.[MIMETYPE]?.['detach']
                        );
                        if (detachPret) {
                            listener((notebook.parent as NotebookPanel).context.path, index);
                        }
                    }
                }
            };
            callbacks.push({callback, signal})
            signal.connect(callback)
        })
        changes.oldValues.forEach((cell: ICodeCellModel) => {
            const oldSignal = cell?.outputs?.changed;
            if (!oldSignal)
                return
            callbacks = callbacks.filter(({callback, signal}) => {
                if (signal === oldSignal) {
                    signal.disconnect(callback);
                    return false;
                }
                return true;
            });
        })
        //if (change.type == "remove") {
        //
        //}
        // for (const cell of sender.widgets) {
        //     if (cell.model.type === 'code' && (cell as CodeCell).outputArea) {
        //         const signal = (cell as CodeCell).outputArea.outputTracker.widgetAdded;
        //         signal.connect((...args) => {
        //             return listener(cell, (cell as CodeCell).outputArea)
        //         });
        //     }
        // }
    })
}

/*
Activate the extension:
-
 */
async function activatePretExtension(
    app: JupyterFrontEnd,
    rendermime: IRenderMimeRegistry,
    docManager: IDocumentManager,
    notebookTracker: INotebookTracker,
    settingRegistry: ISettingRegistry,
    menu: IMainMenu,
    loggerRegistry: ILoggerRegistry | null,
    restorer: ILayoutRestorer | null,
    //palette: ICommandPalette,
) {
    const {commands, shell, contextMenu} = app;
    const pretAreas = new WidgetTracker<MainAreaWidget<PretClonedArea>>({
        namespace: 'pret-areas'
    });

    if (restorer) {
        restorer.restore(pretAreas, {
            command: 'pret:create-view',
            args: widget => ({
                path: widget.content.path,
                index: widget.content.index,
            }),
            name: widget => `${widget.content.path}:${widget.content.index}`,
            when: notebookTracker.restored // After the notebook widgets (but not contents).
        });
    }

    const bindUnhandledIOPubMessageSignal = (nb) => {
        if (!loggerRegistry) {
            return;
        }

        const wManager = contextToPretJupyterHandlerRegistry[nb.context];
        // Don't know what it is
        if (wManager) {
            wManager.onUnhandledIOPubMessage.connect(
                (sender, msg) => {
                    const logger = loggerRegistry.getLogger(nb.context.path);
                    let level: LogLevel = 'warning';
                    if (
                        KernelMessage.isErrorMsg(msg) ||
                        (KernelMessage.isStreamMsg(msg) && msg.content.name === 'stderr')
                    ) {
                        level = 'error';
                    }
                    const data = {
                        ...msg.content,
                        output_type: msg.header.msg_type
                    };
                    logger.rendermime = nb.content.rendermime;
                    logger.log({type: 'output', data, level});
                }
            );
        }
    };

    // Some settings stuff, haven't used it yet
    if (settingRegistry !== null) {
        settingRegistry
            .load(plugin.id)
            .then((settings) => {
                settings.changed.connect(updateSettings);
                updateSettings(settings);
            })
            .catch((reason) => {
                console.error(reason.message);
            });
    }

    // Sets the renderer everytime we see our special SpanComponent/TableEditor mimetype
    rendermime.addFactory(
        {
            safe: false,
            mimeTypes: [MIMETYPE],
            // @ts-ignore
            createRenderer: (options => {
                new PretViewWidget(options, null);
            })
        },
        0
    );

    // Adds the singleton PretJupyterHandler to all existing widgets in the labapp/notebook
    if (notebookTracker !== null) {
        notebookTracker.forEach((panel) => {
            registerPretJupyterHandler(
                panel.context,
                panel.content.rendermime,
                chain(
                    // @ts-ignore
                    getWidgetsFromNotebook(panel.content),
                    getLinkedWidgetsFromApp(app, panel.sessionContext.path)
                )
            );

            bindUnhandledIOPubMessageSignal(panel);
        });
        notebookTracker.widgetAdded.connect((sender, panel: NotebookPanel) => {
            registerPretJupyterHandler(
                panel.context,
                panel.content.rendermime,
                chain(
                    getWidgetsFromNotebook(panel.content),
                    getLinkedWidgetsFromApp(app, panel.sessionContext.path)
                )
            );
            bindUnhandledIOPubMessageSignal(panel);
        });
        notebookTracker.currentChanged.connect((sender, panel: NotebookPanel) => {
            registerOutputListener(
                panel.content,
                (path, index) => {
                    (commands as CommandRegistry).execute('pret:create-view', {path, index});
                    (panel.content.widgets[index] as CodeCell).outputHidden = true;
                },
            )
        });
    }

    const widgetTracker = new WidgetTracker<OutputArea>({namespace: ''});

    /*if (widgetTracker !== null) {
        widgetTracker.widgetAdded.connect((sender, widget) => {
            console.log(sender, widget);
        })
    }*/

    // -----------------
    // Add some commands
    // -----------------

    if (settingRegistry !== null) {
        // Add a command for automatically saving pret state.
        commands.addCommand('pret:saveAnnotatorState', {
            label: 'Save Annotator State Automatically',
            execute: () => {
                return settingRegistry
                    .set(plugin.id, 'saveState', !SETTINGS.saveState)
                    .catch((reason) => {
                        console.error(`Failed to set ${plugin.id}: ${reason.message}`);
                    });
            },
            isToggled: () => SETTINGS.saveState
        });
    }

    if (menu) {
        menu.settingsMenu.addGroup([
            {command: 'pret:saveAnnotatorState'}
        ]);
    }

    /**
     * Whether there is an active notebook.
     */
    function isEnabled() {  // : boolean
        return (
            notebookTracker.currentWidget !== null &&
            notebookTracker.currentWidget === shell.currentWidget
        );
    }

    /**
     * Whether there is a notebook active, with a single selected cell.
     */
    function isEnabledAndSingleSelected() {  // :boolean
        if (!isEnabled()) {
            return false;
        }
        const {content} = notebookTracker.currentWidget;
        const index = content.activeCellIndex;
        // If there are selections that are not the active cell,
        // this command is confusing, so disable it.
        for (let i = 0; i < content.widgets.length; ++i) {
            if (content.isSelected(content.widgets[i]) && i !== index) {
                return false;
            }
        }
        return true;
    }

    // CodeCell context menu groups
    contextMenu.addItem({
        command: 'pret:create-view',
        selector: '.jp-Notebook .jp-CodeCell',
        rank: 10.5,
    });
    commands.addCommand('pret:create-view', {
        label: 'Detach',
        execute: async args => {
            let cell: CodeCell | undefined;
            let current: NotebookPanel | undefined | null;
            // If we are given a notebook path and cell index, then
            // use that, otherwise use the current active cell.
            const path = args.path as string | undefined | null;
            let index = args.index as number | undefined | null;
            if (path && index !== undefined && index !== null) {
                current = docManager.findWidget(path, 'Notebook') as any;
                if (!current) {
                    return;
                }
            } else {
                current = notebookTracker.currentWidget;
                if (!current) {
                    return;
                }
                cell = current.content.activeCell as CodeCell;
                index = current.content.activeCellIndex;
            }
            // Create a MainAreaWidget
            const content = new PretClonedArea({
                notebook: current,
                cell,
                index,
            });

            // Check if it already exists
            const hasBeenDetached = !!pretAreas.find(widget =>
                (widget.content.path === path)
                && (widget.content.index === index)
            )

            if (hasBeenDetached) {
                return;
            }

            const widget = new MainAreaWidget({content});
            current.context.addSibling(widget, {
                ref: current.id,
                mode: 'split-bottom'
            });

            const updateCloned = () => {
                void pretAreas.save(widget);
            };

            current.context.pathChanged.connect(updateCloned);
            current.context.model?.cells.changed.connect(updateCloned);

            // Add the cloned output to the output widget tracker.
            void pretAreas.add(widget);
            void pretAreas.save(widget);

            // Remove the output view if the parent notebook is closed.
            current.content.disposed.connect(() => {
                current!.context.pathChanged.disconnect(updateCloned);
                current!.context.model?.cells.changed.disconnect(updateCloned);
                widget.dispose();
            });
            await Promise.all([
                commands.execute("notebook:hide-cell-outputs", args),
            ]);
        },
        isEnabled: isEnabledAndSingleSelected
    });

    return null;
}

function updateSettings(settings) {
    SETTINGS.saveState = !!settings.get('saveState').composite;
}

const plugin: JupyterFrontEndPlugin<null> = {
    id: 'pret:plugin', // app
    requires: [
        IRenderMimeRegistry,  // rendermime
        IDocumentManager as any,  // docManager
    ],
    optional: [
        INotebookTracker, // notebookTracker
        ISettingRegistry, // settingRegistry
        IMainMenu, // menu
        ILoggerRegistry, // loggerRegistry
        ILayoutRestorer, // restorer
    ],
    activate: activatePretExtension,
    autoStart: true
};

export default plugin;
