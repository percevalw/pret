import type {ISessionContext} from "@jupyterlab/apputils";

export interface IPretHost {
  render(viewReference: {
    serialized?: [Uint8Array, string];
    marshaler_id: string;
    chunk_idx: number;
  }): Promise<void>;
  dispose(): void;
}

export interface IPretService {
  createHost(sessionContext: ISessionContext, node: HTMLElement): IPretHost;
}

export const IPretService: any;

export type {
  ClientDataFrame, DataFrameProvider, DataFrameDescription, QueryOptions, QueryResult,
  QueryScope, DataFrameView, DataFrameRowSource,
} from "../lib/dataframe";

export { useDataFrameView } from "../lib/dataframe";
export { useSnapshot } from "../lib/store";
