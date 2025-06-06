"use strict";
const fs = require("fs");
const path = require("path");
const ts = require(path.join(process.cwd(), "node_modules", "typescript"));

/**
 * Convert a name to snake_case
 * Examples:
 *  - "FooBar" -> "foo_bar"
 *  - "fooBar" -> "foo_bar"
 *  - "foo_bar" -> "foo_bar"
 *  - "foo-bar" -> "foo_bar"
 *  - "dangerouslySetInnerHTML" -> "dangerously_set_inner_html"
 * @param name
 */
function convertToSnakeCase(name) {
  const newName = name
    .replace(/(?<![A-Z]|^)([A-Z])/g, "_$1")
    .replace(/[- ]/g, "_")
    .toLowerCase();
  // Check for python keywords
  if (["and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield"].includes(newName)) {
    return `${newName}_`;
  }
  return newName;
}

/**
 * Convert a TypeScript type annotation to a Python type annotation
 *
 * @param rootType
 * @returns {any|string}
 */
function convertTypeAnnotation(rootType, name) {
  const getUnionType = (type) => {
    // @ts-ignore
    switch (type.flags) {
      case ts.TypeFlags.String:
        return ["str"];
      case ts.TypeFlags.Boolean:
        return ["bool"];
      case ts.TypeFlags.BooleanLiteral:
        // @ts-ignore
        return type.intrinsicName === "true" ? ["Literal[True]"] : ["Literal[False]"];
      case ts.TypeFlags.StringLiteral:
        // @ts-ignore
        return [`Literal["${type.value}"]`];
      case ts.TypeFlags.NumberLiteral:
        // @ts-ignore
        return [`Literal[${type.value}]`];
      case ts.TypeFlags.Number:
        return ["int", "float"];
      case ts.TypeFlags.Null:
      case ts.TypeFlags.Undefined:
        return ["None"];
      default:
        // @ts-ignore
        if (type.types && type.isUnion()) {
          // @ts-ignore
          const result = type.types.map(getUnionType).flat();
          // Check for bool defined as literals
          // Check for other literals that we can group as one and add to the union
          let literals = result.filter((x) => x.startsWith("Literal["));
          if (literals.length > 0) {
            let nonLiterals = result.filter((x) => !x.startsWith("Literal["));
            if (literals.includes("Literal[True]") && result.includes("Literal[False]")) {
              nonLiterals.push("bool");
              literals = literals.filter((x) => x !== "Literal[True]" && x !== "Literal[False]");
            }
            if (literals.length === 0) {
              return nonLiterals;
            }
            const literalTypes = literals.map((x) => x.replace("Literal[", "").replace("]", ""));
            return Array.from(new Set([...nonLiterals, `Literal[${literalTypes.join(", ")}]`]));
          }
        }
        // Check for React.key
        try {
          if (type.aliasSymbol.name === "Key" && type.aliasSymbol.parent.name === "React") {
            return ["str", "int"];
          }
        } catch (e) {
        }
        // console.log("Could not resolve type:", this.checker.typeToString(type));
        return ["Any"];
    }
  };
  try {
    // compute and deduplicate union types
    const unionType = Array.from(new Set(getUnionType(rootType)));
    if (unionType.length === 1) {
      return unionType[0];
    }
    return `Union[${unionType.join(", ")}]`;
  } catch (e) {
    console.error(e);
    return "Any";
  }
}

/**
 * Generate a stub function for a React component
 * @param componentName
 * @param jsModuleName
 * @param props
 * @param jsDoc
 * @returns {string}
 */
function generateComponentStubFunction(componentName, jsModuleName, props, jsDoc) {
  const propsByName = Object.fromEntries(props.map(([name, type, doc]) => [name, { "type": type, "doc": doc }]));
  // Turn parameters into a function signature
  const typedPropsString = Array
    .from(new Set(props.map(([name]) => name)))
    .map(name => `${name}: ${propsByName[name]["type"]}`)
    .join(", ");
  // Add Parameters section to the docstring
  let paramDocs = Array
    .from(new Set(props.map(([name]) => name)))
    .filter(name => propsByName[name]["doc"])
    .map(name => `${name}: ${propsByName[name]["type"]}` + (propsByName[name]["doc"] ? `\n    ${propsByName[name]["doc"].trim()}` : ""))
    .join("\n");
  if (paramDocs.length > 0) {
    jsDoc = jsDoc + `\n\nParameters\n----------\n${paramDocs}`;
  }
  // Indent docstring
  jsDoc = jsDoc ? "\n" + jsDoc.split("\n").map((line) => `    ${line}`).join("\n") + "\n" : "";
  return (`@stub_component(js.${jsModuleName}.${componentName}, props_mapping)
def ${componentName}(*children, ${typedPropsString}):
    """${jsDoc || ""}"""`);
}

/**
 * Generate a stub function a JS function
 * @param functionName
 * @param jsModuleName
 * @param jsFunctionName
 * @param props
 * @param jsDoc
 * @returns {string}
 */
function generateStubFunction(functionName, jsModuleName, jsFunctionName, props, jsDoc) {
  // deduplicate props by name
  const propsByName = Object.fromEntries(props.map(([name, type]) => [name, type]));
  const typedPropsString = Array
    .from(new Set(props.map(([name]) => name)))
    .map(name => !name.startsWith("*")
      ? `${name}: ${propsByName[name]}=None`
      : `${name}: ${propsByName[name]}`)
    .join(", ");
  return (`
@marshal_as(js.${jsModuleName}.${jsFunctionName})
def ${functionName}(${typedPropsString}):
    """${jsDoc || ""}"""`);
}


function findNode(node, position) {
  if (position >= node.getStart() && position <= node.getEnd()) {
    return ts.forEachChild(node, c => findNode(c, position)) || node;
  }
}

function isRestParameter(node) {
  const type = ts.isJSDocParameterTag(node) ? (node.typeExpression && node.typeExpression.type) : node.type;
  return node.dotDotDotToken !== undefined || !!type && type.kind === ts.SyntaxKind.JSDocVariadicType;
}


/**
 * Get the modules available for import
 * @param sourceString
 * @returns {string[]|*[]}
 */
function getImported(sourceString) {
  const languageService = makeLanguageService(sourceString);
  const prompt = `<MODULE.`;
  const autocompletePosition = sourceString.indexOf(prompt) + prompt.length;
  const completions = languageService.getCompletionsAtPosition("@fake.tsx", autocompletePosition, {
    includeCompletionsForModuleExports: true,
    includeCompletionsWithInsertText: true
  });
  if (!completions) {
    return [];
  }
  return completions.entries
    .map((entry) => entry.name);
  //.filter((name) => name == "default" || name[0] === name[0].toUpperCase());
}


/**
 * Get closest package JSON content
 * @param sourceString
 * @returns Object
 */
function getClosestPackageJSON(packageName) {
  const sourceString = `
import "${packageName}";
`;
  const languageService = makeLanguageService(sourceString);
  const definition = languageService.getDefinitionAtPosition("@fake.tsx", 9);

  /** For instance if "packageName" is "../src/lib" above, we get quickInfo
   * [
   *   {
   *     fileName: '/Users/perceval/Development/react-simple-dock/src/lib/index.tsx',
   *     textSpan: { start: 0, length: 0 },
   *     kind: 'script',
   *     name: './src/lib',
   *     containerName: undefined,
   *     containerKind: undefined,
   *     unverified: false
   *   }
   * ]
   * */

  if (!definition || definition.length === 0) {
    return;
  }
  let path = definition[0].fileName;

  while (true) {
    const packageJSONPath = path + "/package.json";
    if (fs.existsSync(packageJSONPath)) {
      return JSON.parse(fs.readFileSync(packageJSONPath, "utf8"));
    }
    const newPath = path.split("/");
    newPath.pop();
    path = newPath.join("/");
  }
}


/**
 * Get the props/arguments for a function
 * @param name
 * @param languageService
 * @param sourceString
 * @param snakeCaseMapping
 * @returns {{doc: null, props: null}|{doc: string, props: unknown[]}}
 */
function getFunctionProps(name, sourceString, snakeCaseMapping) {
  const languageService = makeLanguageService(sourceString);
  // We can't use autocomplete now so we
  // get the type of the below prompt using the checker
  const prompt = `MODULE.${name}`;
  const position = sourceString.indexOf(prompt) + prompt.length;
  const program = languageService.getProgram();
  const checker = program.getTypeChecker();
  // Get existing symbol now at our position
  const node = findNode(program.getSourceFile("@fake.tsx"), position);
  const symbol = checker.getSymbolAtLocation(node);
  const type = checker.getTypeOfSymbolAtLocation(symbol, node);
  if (type.getCallSignatures().length == 0) {
    return { props: null, doc: null };
  }
  const jsDoc = ts.displayPartsToString(symbol.getDocumentationComment(checker));
  const paramNameToDoc = {};
  const jsDocTags = type.symbol ? type.symbol.getJsDocTags(checker) : [];
  for (const tag of jsDocTags) {
    if (tag.name === "param") {
      // param name is the first part of the .text array field with kind `parameterName`
      let paramName = tag.text[0].text;
      // doc of the param is all the parts of the .text array field with kind `text`
      let paramDoc = tag.text.map((part) => part.kind === "text" ? part.text : "").join(" ");
      paramNameToDoc[paramName] = paramDoc.trim();
    }
  }
  // Get each argument's name and type
  return {
    "props": type.getCallSignatures()[0].getParameters().map((param) => {
      const name = param.getName();
      const type = checker.getTypeOfSymbolAtLocation(param, param.valueDeclaration);
      // check if it is a rest parameter
      const arg_name = convertToSnakeCase(name);
      if (arg_name !== name) {
        snakeCaseMapping[arg_name] = name;
      }
      if (isRestParameter(param.valueDeclaration)) {
        const subType = checker.getTypeArguments(type)[0];
        return [`*${arg_name}`, convertTypeAnnotation(subType, arg_name)];
      } else {
        return [arg_name, convertTypeAnnotation(type, arg_name), paramNameToDoc[name] || ""];
      }
    }),
    "doc": jsDoc
  };
}

/**
 * Extract the props from a React component
 * @param name
 * @param sourceString
 * @param snakeCaseMapping
 * @returns {{doc: null, props: null}|{doc: string, props: [(string|string),(*|string)][]}}
 */
function getComponentProps(name, sourceString, snakeCaseMapping) {
  const languageService = makeLanguageService(sourceString);
  const prompt = `<MODULE.${name} `;
  const autocompletePosition = sourceString.indexOf(prompt) + prompt.length;
  const completions = languageService.getCompletionsAtPosition("@fake.tsx", autocompletePosition, {
    includeCompletionsForModuleExports: true,
    includeCompletionsWithInsertText: true
  });
  if (!completions) {
    return { props: null, doc: null };
  }
  const program = languageService.getProgram();
  const checker = program.getTypeChecker();
  const node = findNode(languageService.getProgram().getSourceFile("@fake.tsx"), autocompletePosition - 1);
  const symbol = checker.getSymbolAtLocation(node);
  const type = checker.getTypeOfSymbolAtLocation(symbol, node);
  const jsDoc = type.symbol
    ? ts.displayPartsToString(symbol.getDocumentationComment(checker))
    : null;
  const paramNameToDoc = {};
  const jsDocTags = type.symbol ? type.symbol.getJsDocTags(checker) : [];
  for (const tag of jsDocTags) {
    if (tag.name === "param") {
      // param name is the first part of the .text array field with kind `parameterName`
      let paramName = tag.text[0].text;
      if (paramName.startsWith("props.")) {
        paramName = paramName.substring(6);
      }
      // doc of the param is all the parts of the .text array field with kind `text`
      let paramDoc = tag.text.map((part) => part.kind === "text" ? part.text : "").join(" ");
      paramNameToDoc[paramName] = paramDoc.trim();
    }
  }
  return {
    props: completions.entries
      // in ts < 5, we must check if kind is ts.ScriptElementKind.jsxAttribute
      // in ts >= 5, we must check if kind is "property"
      .filter(entry => (entry.kind === "property" || entry.kind === ts.ScriptElementKind.jsxAttribute) && entry.name !== "children")
      .map((entry) => {
        const symbol = languageService.getCompletionEntrySymbol("@fake.tsx", autocompletePosition, entry.name, undefined);
        const checker = languageService.getProgram().getTypeChecker();
        const type = checker.getTypeOfSymbolAtLocation(symbol, symbol.valueDeclaration);
        const arg_name = convertToSnakeCase(entry.name);
        if (arg_name !== entry.name) {
          snakeCaseMapping[arg_name] = entry.name;
        }
        return [arg_name, convertTypeAnnotation(type, arg_name), paramNameToDoc[entry.name] || ""];
      }),
    doc: jsDoc
  };
}


const options = {
  jsx: ts.JsxEmit.React,
  module: ts.ModuleKind.ESNext,
  target: ts.ScriptTarget.ESNext,
  baseUrl: ".",
  moduleResolution: ts.ModuleResolutionKind.NodeJs,
  allowSyntheticDefaultImports: true
};

function makeLanguageService(sourceString) {
  return ts.createLanguageService({
    getScriptFileNames: () => ["@fake.tsx"],
    getScriptVersion: () => "1.0",
    getScriptSnapshot: (fileName) => {
      if (fileName === "@fake.tsx") {
        return ts.ScriptSnapshot.fromString(sourceString);
      }
      if (fs.existsSync(fileName)) {
        const fileContent = fs.readFileSync(fileName, "utf8");
        return ts.ScriptSnapshot.fromString(fileContent);
      }
      throw new Error(`File not found: ${fileName}`);
    },
    getCurrentDirectory: () => process.cwd(),
    getCompilationSettings: () => options,
    getDefaultLibFileName: (options) => ts.getDefaultLibFilePath(options),
    fileExists: ts.sys.fileExists,
    readFile: ts.sys.readFile,
    readDirectory: ts.sys.readDirectory,
    getDirectories: ts.sys.getDirectories
  }, ts.createDocumentRegistry());
}

/**
 * Run the stub generator
 * @param packageName: The name of the package
 * @param jsModuleName: The name of the global client alias for the package
 * @param outputPath: The path to write the python stubs to
 */
const run = (packagePath, pyPackageName, globalName, outputPath) => {
  const snakeCaseMapping = {};
  let sourceString = "";

  // Extract module name and version from package.json

  let packageJSON, packageName, version;

  try {
    packageJSON = getClosestPackageJSON(packagePath);
    packageName = packageJSON.name;
    version = packageJSON?.version || "0.0.0";
  } catch (e) {
    console.error("Could not find package.json for", packagePath);
    process.exit(1);
  }

  // Extract all potential components from the module
  sourceString = `
import React from "react";
import * as MODULE from "${packagePath}";

<MODULE. />;
`;
  let imported = getImported(sourceString);
  console.log(`Found ${imported.length} potential components & functions`);
  // Filter out non-react components
  sourceString = (`
import React from "react";
import * as MODULE from "${packagePath}";`
    + `\n\n` + imported.map((name) => `<MODULE.${name} />`).join("\n")
    + `\n\n` + imported.map((name) => `MODULE.${name}( )`).join("\n"));
  console.log("Running type checker on potential components & functions");
  const diagnostics = makeLanguageService(sourceString).getSemanticDiagnostics("@fake.tsx");
  const nonReactComponents = diagnostics
    .filter(d => d.code === 2786 || d.code === 2604 || d.code == 2322)
    .map(d => sourceString.substring(d.start + 7, d.start + d.length));
  const nonFunctions = diagnostics
    .filter(d => d.code === 2349)
    .map(d => sourceString.substring(d.start, d.start + d.length));
  // Generate stubs
  const stubBody = (imported
      .filter(c => !nonReactComponents.includes(c))
      .map((componentName) => {
        const { props, doc } = getComponentProps(componentName, sourceString, snakeCaseMapping);
        if (!props) {
          console.log("Could not generate stub for component", componentName);
          return;
        }
        nonFunctions.push(componentName);
        return generateComponentStubFunction(componentName, globalName, props, doc);
      }).join("\n")
    + "\n\n" +
    imported
      .filter(c => !nonFunctions.includes(c))
      .map((name) => {
        const { props, doc } = getFunctionProps(name, sourceString, snakeCaseMapping);
        if (!props) {
          console.log("Could not generate stub for component", name);
          return;
        }
        const snakeCaseName = convertToSnakeCase(name);
        return generateStubFunction(snakeCaseName, globalName, name, props, doc);
      }).join("\n"));
  const moduleString = `
import sys
from typing import Any, Union, List
from pret.render import stub_component
from pret.marshal import js, make_stub_js_module, marshal_as

__version__ = "${version}"
_py_package_name = "${pyPackageName}"
_js_package_name = "${packageName}"
_js_global_name = "${globalName}"

make_stub_js_module("${globalName}", "${pyPackageName}", "${packageName}", __version__, __name__)

if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal

props_mapping = ${JSON.stringify(snakeCaseMapping, null, 1)}

${stubBody}
`;
  if (outputPath) {
    fs.writeFileSync(outputPath, moduleString);
  } else {
    console.log(moduleString);
  }
};

/*
// @ts-ignore
global.ts = ts;

repl.start({
    useGlobal: true,
    prompt: "> ",
    input: process.stdin,
    output: process.stdout,
});*/
function main() {
  // Get command-line arguments
  const args = process.argv.slice(2);
  // Validate command-line arguments
  if (args.length < 1 || args.length > 4) {
    console.error("Usage: node index.js <packagePath> <pyPackageName> <globalModuleName> <outputPath>");
    process.exit(1);
  }
  // Run StubGenerator with the input and output paths
  const packagePath = args[0];
  const pyPackageName = args.length >= 2 ? args[1] : undefined;
  const jsPackageName = args.length >= 3 ? args[2] : undefined;
  const outputPath = args.length >= 4 ? args[3] : undefined;
  run(packagePath, pyPackageName, jsPackageName, outputPath);
}

// Check if main, without using require
main();
