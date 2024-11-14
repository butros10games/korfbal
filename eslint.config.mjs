import globals from "globals";
import js from "@eslint/js";

/** @type {import('eslint').Linter.Config[]} */
export default [
	js.configs.recommended,
	{
		files: ["static_workfile/js/**/*.js"],
		rules: {
			"camelcase": ["off", { "properties": "always" }],
			"comma-spacing": ["error", { "before": false, "after": true }],
			"curly": ["error", "all"],
			"dot-notation": ["error", { "allowKeywords": true }],
			"eqeqeq": ["error"],
			"indent": ["error", 4],
			"key-spacing": ["error", { "beforeColon": false, "afterColon": true }],
			"linebreak-style": ["error", "unix"],
			"new-cap": ["off", { "newIsCap": true, "capIsNew": true }],
			"no-alert": ["off"],
			"no-eval": ["error"],
			"no-extend-native": ["error", { "exceptions": ["Date", "String"] }],
			"no-multi-spaces": ["error"],
			"no-octal-escape": ["error"],
			"no-script-url": ["error"],
			"no-shadow": ["error", { "hoist": "functions" }],
			"no-underscore-dangle": ["error"],
			"no-unused-vars": ["error", { "vars": "local", "args": "none" }],
			"no-var": ["error"],
			"prefer-const": ["error"],
			"quotes": ["off", "single"],
			"semi": ["error", "always"],
			"space-before-blocks": ["error", "always"],
			"space-before-function-paren": ["error", { "anonymous": "never", "named": "never" }],
			"space-infix-ops": ["error", { "int32Hint": false }],
			"strict": ["error", "global"],
		},
		languageOptions: {
		ecmaVersion: "latest",
		sourceType: "script",
		globals: {
			...globals.browser,
			...globals.commonjs,
			django: false,
		},
		},
	},
	{
		files: ["static_workfile/js/**/*.mjs"],
		languageOptions: {
			sourceType: "module",
		},
	},
	{
		ignores: [
			"static_workfile/js/**/*.min.js",
			"static_workfile/js/vendor/**/*.js",
			"node_modules/**",
			"tests/**/*.js",
		],
	},
];
