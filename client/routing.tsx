export {
  Outlet,
  useMatch,
  useParams,
  Routes,
  Route,
  useLocation,
} from "react-router";
export { useSearchParams } from "react-router-dom";
import {
  BrowserRouter as BaseBrowserRouter,
  HashRouter as BaseHashRouter,
} from "react-router-dom";

import { useEffect, RefObject, useRef } from "react";
import { useNavigate, useInRouterContext } from "react-router-dom";

type Options = {
  containerRef?: RefObject<HTMLElement>;
  basename?: string;
  exclude_attr?: string; // ex "data-router-ignore"
};

export function useInterceptAnchorsClicks({
  containerRef,
  basename = "",
  exclude_attr = "data-router-ignore",
}: Options = {}) {
  const navigate = useNavigate();

  useEffect(() => {
    const root = containerRef?.current;
    if (!root) return;

    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

      const a = (e.target as Element | null)?.closest?.(
        "a[href]"
      ) as HTMLAnchorElement | null;
      if (!a) return;

      if (a.hasAttribute(exclude_attr)) return;
      if (a.hasAttribute("download")) return;

      const tgt = (a.getAttribute("target") || "").toLowerCase();
      if (tgt && tgt !== "_self") return;
      if ((a.getAttribute("rel") || "").includes("external")) return;

      const href = a.getAttribute("href") || "";
      if (href.startsWith("#")) return;
      if (/^(mailto|tel|ftp|blob|data|javascript):/i.test(href)) return;
      const url = new URL(href, window.location.href);
      if (url.origin !== window.location.origin) return;

      // get the rel path
      let pathname = url.pathname;
      if (basename && pathname.startsWith(basename)) {
        pathname = pathname.slice(basename.length) || "/";
      }

      e.preventDefault();
      navigate(pathname + url.search + url.hash);
    };

    // capture to handle the click before other handles
    root.addEventListener("click", onClick, { capture: true });
    return () => root.removeEventListener("click", onClick, { capture: true });
  }, [navigate, containerRef, basename, exclude_attr]);
}

function AnchorClickInterceptor(props: Options) {
  useInterceptAnchorsClicks(props);
  return null;
}

export function BrowserRouter(
  props: {
    interceptAnchorClicks?:
      | boolean
      | {
          basename?: string;
          exclude_attr?: string;
        };
  } & React.ComponentProps<typeof BaseBrowserRouter>
) {
  const { interceptAnchorClicks, children, ...rest } = props;

  if (!interceptAnchorClicks) {
    return <BaseBrowserRouter {...rest}>{children}</BaseBrowserRouter>;
  }

  const containerRef = useRef(null);
  const basename =
    (typeof interceptAnchorClicks === "object" &&
      interceptAnchorClicks.basename) ||
    props.basename ||
    "";
  const exclude_attr =
    (typeof interceptAnchorClicks === "object" &&
      interceptAnchorClicks.exclude_attr) ||
    "data-router-ignore";

  return (
    <BaseBrowserRouter {...{ future: { v7_startTransition: true }, ...rest }}>
      <AnchorClickInterceptor
        containerRef={containerRef}
        basename={basename}
        exclude_attr={exclude_attr}
      />
      <div ref={containerRef}>{children}</div>
    </BaseBrowserRouter>
  );
}


export function HashRouter(
  props: {
    interceptAnchorClicks?:
      | boolean
      | {
          basename?: string;
          exclude_attr?: string;
        };
  } & React.ComponentProps<typeof BaseHashRouter>
) {
  const { interceptAnchorClicks, children, ...rest } = props;

  if (!interceptAnchorClicks) {
    return <BaseHashRouter {...rest}>{children}</BaseHashRouter>;
  }

  const containerRef = useRef(null);
  const basename =
    (typeof interceptAnchorClicks === "object" &&
      interceptAnchorClicks.basename) ||
    props.basename ||
    "";
  const exclude_attr =
    (typeof interceptAnchorClicks === "object" &&
      interceptAnchorClicks.exclude_attr) ||
    "data-router-ignore";

  return (
    <BaseHashRouter {...{ future: { v7_startTransition: true }, ...rest }}>
      <AnchorClickInterceptor
        containerRef={containerRef}
        basename={basename}
        exclude_attr={exclude_attr}
      />
      <div ref={containerRef}>{children}</div>
    </BaseHashRouter>
  );
}
