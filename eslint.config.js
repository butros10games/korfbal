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
            "indent": ["error", 4, { "SwitchCase": 1 }],
            "key-spacing": ["error", { "beforeColon": false, "afterColon": true }],
            "linebreak-style": ["error", "unix"],
            "new-cap": ["off", { "newIsCap": true, "capIsNew": true }],
            "no-alert": ["off"],
            "no-console": "warn",
            "no-debugger": "error",
            "no-eval": ["error"],
            "no-extend-native": ["error", { "exceptions": ["Date", "String"] }],
            "no-multi-spaces": ["error"],
            "no-octal-escape": ["error"],
            "no-script-url": ["error"],
            "no-shadow": ["error", { "hoist": "functions" }],
            "no-underscore-dangle": ["off"],
            "no-unused-vars": ["error", { "vars": "local", "args": "none" }],
            "no-var": ["error"],
            "prefer-const": ["error"],
            "quotes": ["error", "single", { "avoidEscape": true }],
            "semi": ["error", "always"],
            "space-before-blocks": ["error", "always"],
            "space-before-function-paren": ["error", { "anonymous": "never", "named": "never" }],
            "space-infix-ops": ["error", { "int32Hint": false }],
            "strict": ["error", "never"],
            "max-len": ["error", {
                "code": 88,
                "tabWidth": 4,
                "ignoreComments": true,
                "ignoreUrls": true,
                "ignoreStrings": true,
                "ignoreTemplateLiterals": true,
                "ignoreRegExpLiterals": true
            }],
            "object-curly-spacing": ["error", "always"],
            "no-trailing-spaces": ["error"],
            "eol-last": ["error", "always"],
            "arrow-spacing": ["error", { "before": true, "after": true }],
            "arrow-body-style": ["error", "as-needed"],
            "prefer-arrow-callback": ["error"]
        },
		languageOptions: {
			ecmaVersion: "latest",
			sourceType: "module",
			globals: {
				...globals.browser,
				...globals.commonjs,
				django: false,
				heic2any: "readonly",
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
		files: ["configs/webpack/webpack.config.js"],
		languageOptions: {
			ecmaVersion: "latest",
			sourceType: "script",
			globals: {
				...globals.node,
			},
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
