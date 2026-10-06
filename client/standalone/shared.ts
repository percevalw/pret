// Runtime exports of the pret package shared with extension bundles, such as Metanno, in standalone apps,
// which have no Jupyter service. The modules are those of the standalone app, so their state is shared
export { useDataFrameView } from "../dataframe";
export { useSnapshot } from "../store";
