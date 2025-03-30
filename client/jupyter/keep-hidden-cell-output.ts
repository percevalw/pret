/* -----------------------------------------------------------------------------
| Modified by PW from the JupyterLab repository with an ugly monkey patch hack
| Copyright (c) Jupyter Development Team.
| Distributed under the terms of the Modified BSD License.
|----------------------------------------------------------------------------*/

//@ts-ignore
import { OutputArea } from "@jupyterlab/outputarea";
//@ts-ignore
import { CodeCell, ICodeCellModel } from "@jupyterlab/cells";
//@ts-ignore
import { ISessionContext } from "@jupyterlab/apputils";
//@ts-ignore
import { Kernel, KernelMessage } from "@jupyterlab/services";
//@ts-ignore
import { JSONObject } from "@lumino/coreutils";

/**
 * The namespace for the `CodeCell` class statics.
 */

/**
 * Execute a cell given a client session.
 */

(function (CodeCell) {
  async function execute(
    cell: CodeCell,
    sessionContext: ISessionContext,
    metadata?: JSONObject
  ): Promise<KernelMessage.IExecuteReplyMsg | void> {
    const model: any /* ICodeCellModel */ = cell.model;
    const code = model.sharedModel
      ? model.sharedModel.getSource()
      : model.value.text;
    const canChangeHiddenState = !(cell?.outputArea?.widgets?.[0] as any)
      ?.widgets?.[1]?.keepHiddenWhenExecuted;
    // ^--- modified here
    if (!code.trim() || !sessionContext.session?.kernel) {
      if (model.sharedModel) {
        model.sharedModel.transact(
          () => {
            model.clearExecution();
          },
          false,
          "silent-change"
        );
      } else {
        model.clearExecution();
      }
      return;
    }
    const cellId = {
      cellId: model.sharedModel ? model.sharedModel.getId() : model.id,
    };
    metadata = {
      ...(model.metadata.toJSON ? model.metadata.toJSON() : model.metadata),
      ...metadata,
      ...cellId,
    };
    const { recordTiming } = metadata;
    if (model.sharedModel) {
      model.sharedModel.transact(
        () => {
          model.clearExecution();
          if (canChangeHiddenState) {
            cell.outputHidden = false;
          }
        },
        false,
        "silent-change"
      );
    } else {
      model.clearExecution();
      // modified here: wrapped in a if statement here
      if (canChangeHiddenState) {
        cell.outputHidden = false;
      }
    }
    if (cell.setPrompt) {
      cell.setPrompt("*");
    } else {
      model.executionState = "running";
    }
    model.trusted = true;
    let future:
      | Kernel.IFuture<
          KernelMessage.IExecuteRequestMsg,
          KernelMessage.IExecuteReplyMsg
        >
      | undefined;
    try {
      const msgPromise = OutputArea.execute(
        code,
        cell.outputArea,
        sessionContext,
        metadata
      );
      // cell.outputArea.future assigned synchronously in `execute`
      if (recordTiming) {
        const recordTimingHook = (msg: KernelMessage.IIOPubMessage) => {
          let label: string;
          switch (msg.header.msg_type) {
            case "status":
              label = `status.${
                (msg as KernelMessage.IStatusMsg).content.execution_state
              }`;
              break;
            case "execute_input":
              label = "execute_input";
              break;
            default:
              return true;
          }
          // If the data is missing, estimate it to now
          // Date was added in 5.1: https://jupyter-client.readthedocs.io/en/stable/messaging.html#message-header
          const value = msg.header.date || new Date().toISOString();
          const timingInfo: any = Object.assign(
            {},

            model.getMetadata
              ? model.getMetadata("execution")
              : model.metadata.get("execution")
          );
          timingInfo[`iopub.${label}`] = value;

          model.setMetadata
            ? model.setMetadata("execution", timingInfo)
            : model.metadata.set("execution", timingInfo);
          return true;
        };
        cell.outputArea.future.registerMessageHook(recordTimingHook);
      } else {
        model.deleteMetadata
          ? model.deleteMetadata("execution")
          : model.metadata.delete("execution");
      }
      // Save this execution's future so we can compare in the catch below.
      future = cell.outputArea.future;
      const msg = (await msgPromise)!;
      model.executionCount = msg.content.execution_count;
      if (recordTiming) {
        const timingInfo = Object.assign(
          {},
          model.getMetadata
            ? (model.getMetadata("execution") as any)
            : (model.metadata.get("execution") as any)
        );
        const started = msg.metadata.started as string;
        // Started is not in the API, but metadata IPyKernel sends
        if (started) {
          timingInfo["shell.execute_reply.started"] = started;
        }
        // Per above, the 5.0 spec does not assume date, so we estimate is required
        const finished = msg.header.date as string;
        timingInfo["shell.execute_reply"] =
          finished || new Date().toISOString();

        model.setMetadata
          ? model.setMetadata("execution", timingInfo)
          : model.metadata.set("execution", timingInfo);
        if (canChangeHiddenState) {
          // <--- modified here
          cell.outputHidden = false;
        } // <--- modified here
      }
      return msg;
    } catch (e) {
      // If we started executing, and the cell is still indicating this
      // execution, clear the prompt.
      if (future && !cell.isDisposed && cell.outputArea.future === future) {
        if (cell.setPrompt) {
          cell.setPrompt("");
        } else {
          model.executionState = "idle";
        }
        if (recordTiming && future.isDisposed) {
          // Record the time when the cell execution was aborted
          const timingInfo: any = Object.assign(
            {},

            model.getMetadata
              ? model.getMetadata("execution")
              : model.metadata.get("execution")
          );
          timingInfo["execution_failed"] = new Date().toISOString();

          model.setMetadata
            ? model.setMetadata("execution", timingInfo)
            : model.metadata.set("execution", timingInfo);
        }
      }
      throw e;
    }
  }

  // @ts-ignore
  CodeCell.old_execute = CodeCell.execute;
  CodeCell.execute = execute;
})(CodeCell);
