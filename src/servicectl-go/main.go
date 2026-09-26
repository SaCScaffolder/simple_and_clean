// Command servicectl-go is a thin Go wrapper around the `servicectl`
// Python CLI.
//
// Why does this exist?
//
//   servicectl is a polyglot platform tool by design: the scaffolder is
//   Python, the templates emit services in Go, Node, Python, and .NET,
//   and the platform tooling itself should reflect that reality rather
//   than fight it. `servicectl-go` is the Go counterpart to the Python
//   CLI: it lets engineers who live in Go (or scripts/CI/chatops that
//   prefer a Go static binary) drive scaffolding without shelling out to
//   Python from scratch each time.
//
// servicectl-go is NOT a re-implementation of the scaffolder. That would
// duplicate the Python logic and drift over time. The Python CLI under
// `src/servicectl/` stays the source of truth; this binary is a typed
// affordance layer on top of it. The mirror pattern is documented in
// `docs/architecture.md` and `src/PACKAGES.md`.
//
// Usage:
//
//	servicectl-go init <name> --template=go-webapi [flags]
//
// Flags mirror the Python CLI. Use --python to point at a specific
// Python interpreter (use `python3` on systems where `python` is Python 2).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"time"
)

const version = "0.1.0"

// Allowed template ids. Mirrors src/servicectl/templates.py and
// src/sdk-ts/src/index.ts. Keep in sync.
const (
	tplNodeExpress    = "node-express"
	tplNodeReactWeb   = "node-react-web"
	tplPythonFlask    = "python-flask"
	tplDotnetWebAPI   = "dotnet-webapi"
	tplGoWebAPI       = "go-webapi"
)

func main() {
	if len(os.Args) < 2 {
		usageTop()
	}
	switch os.Args[1] {
	case "init":
		if err := runInit(os.Args[2:]); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				os.Exit(0)
			}
			fmt.Fprintf(os.Stderr, "servicectl-go: %v\n", err)
			os.Exit(1)
		}
	case "version", "--version", "-v":
		fmt.Printf("servicectl-go v%s\n", version)
	case "help", "--help", "-h":
		usageTop()
	default:
		fmt.Fprintf(os.Stderr, "servicectl-go: unknown subcommand %q\n\n", os.Args[1])
		usageTop()
	}
}

func usageTop() {
	fmt.Fprint(os.Stderr, "Usage: servicectl-go <command> [flags]\n\n"+
		"Commands:\n"+
		"  init     Scaffold a new service (shells out to the servicectl Python CLI)\n"+
		"  version  Print version and exit\n"+
		"  help     Print this message\n\n"+
		"Run 'servicectl-go init --help' for init flags.\n")
	os.Exit(2)
}

// runInit is the heart of the wrapper. It parses flags, validates input,
// then shells out to `python -m servicectl init <name> [flags]`.
func runInit(rawArgs []string) error {
	fs := flag.NewFlagSet("init", flag.ContinueOnError)
	fs.Usage = func() {
		fmt.Fprint(os.Stderr, "Usage: servicectl-go init <name> [flags]\n\n"+
			"Required:\n"+
			"  --template=<node-express|node-react-web|python-flask|dotnet-webapi>\n\n"+
			"Optional:\n"+
			"  --ci=<github-actions|azure-devops>                [default: github-actions]\n"+
			"  --deploy=<local|azure|azure-container-apps>      [default: local]\n"+
			"  --azure-region=<region>                          [default: eastus]\n"+
			"  --coverage=<0-100>                               [default: 80]\n"+
			"  --registry=<dockerhub|ghcr|ecr|acr|gcr>          [default: ghcr]\n"+
			"  --output-dir=<path>                              [default: .]\n"+
			"  --no-git                                         skip git init in the new service\n"+
			"  --no-readme                                      skip README generation\n\n"+
			"Wrapper flags:\n"+
			"  --python=<path>        Python interpreter to invoke   [default: python on PATH]\n"+
			"  --timeout=<duration>   max time to wait for the CLI   [default: 2m]\n"+
			"  --json                 print the resolved config + service root as JSON\n")
	}

	var (
		tpl       = fs.String("template", "", "service template (required): node-express | python-flask | dotnet-webapi | go-webapi")
		ci        = fs.String("ci", "github-actions", "CI provider: github-actions | azure-devops")
		deploy    = fs.String("deploy", "local", "deploy target: local | azure | azure-container-apps")
		azureReg  = fs.String("azure-region", "eastus", "Azure region for deploy targets that need one")
		coverage  = fs.Int("coverage", 80, "minimum test coverage threshold (percent)")
		registry  = fs.String("registry", "ghcr", "container registry: dockerhub | ghcr | ecr | acr | gcr")
		outputDir = fs.String("output-dir", ".", "directory in which to create the service folder")
		noGit     = fs.Bool("no-git", false, "skip `git init` after scaffolding")
		noReadme  = fs.Bool("no-readme", false, "skip README generation (not recommended)")
		python    = fs.String("python", "python", "Python interpreter to invoke (use `python3` if `python` is Python 2)")
		timeout   = fs.Duration("timeout", 2*time.Minute, "max time to wait for the CLI")
		asJSON    = fs.Bool("json", false, "print the resolved config + service root as JSON")
	)

	// Pre-process args: Go's flag package stops parsing at the first
	// non-flag positional. The Python CLI accepts both `init my-svc
	// --template=foo` and `init --template=foo my-svc`. We rearrange so
	// the first positional name is moved to the end, preserving the
	// relative order of all other args. This is the same trick Cobra and
	// many other Go CLIs use.
	name, args := splitName(rawArgs)
	if name == "" {
		fs.Usage()
		return errors.New("missing required argument: <name>")
	}

	if err := fs.Parse(args); err != nil {
		// flag.ErrHelp is returned when --help is passed; flag.PrintDefaults
		// was already called via fs.Usage. Exit cleanly.
		return err
	}

	if !validServiceName(name) {
		return fmt.Errorf("invalid service name %q (allowed: lowercase letters, digits, '-', '_', '.'; no spaces)", name)
	}

	// --template is required. Empty default means user didn't pass it.
	if *tpl == "" {
		return errors.New("missing required flag: --template")
	}
	if !validTemplate(*tpl) {
		return fmt.Errorf("invalid --template %q (allowed: node-express, python-flask, dotnet-webapi, go-webapi)", *tpl)
	}
	if *coverage < 0 || *coverage > 100 {
		return fmt.Errorf("invalid --coverage %d (must be 0-100)", *coverage)
	}

	cfg := resolvedConfig{
		Name:        name,
		Template:    *tpl,
		CI:          *ci,
		Deploy:      *deploy,
		AzureRegion: *azureReg,
		Coverage:    *coverage,
		Registry:    *registry,
		OutputDir:   *outputDir,
		NoGit:       *noGit,
		NoReadme:    *noReadme,
	}

	// Build args for the Python CLI. Mirror the SDK's buildArgs exactly.
	cliArgs := []string{"-m", "servicectl", "init", cfg.Name,
		"--template=" + cfg.Template,
		"--ci=" + cfg.CI,
		"--deploy=" + cfg.Deploy,
		"--azure-region=" + cfg.AzureRegion,
		fmt.Sprintf("--coverage=%d", cfg.Coverage),
		"--registry=" + cfg.Registry,
		"--output-dir=" + cfg.OutputDir,
	}
	if cfg.NoGit {
		cliArgs = append(cliArgs, "--no-git")
	}
	if cfg.NoReadme {
		cliArgs = append(cliArgs, "--no-readme")
	}

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, *python, cliArgs...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return fmt.Errorf("servicectl init timed out after %s", *timeout)
		}
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return fmt.Errorf("servicectl init failed with exit code %d", exitErr.ExitCode())
		}
		return fmt.Errorf("failed to invoke %s: %w", *python, err)
	}

	if *asJSON {
		root, _ := filepath.Abs(filepath.Join(cfg.OutputDir, cfg.Name))
		out := map[string]any{
			"config":      cfg,
			"serviceRoot": root,
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(out)
	}
	return nil
}

// resolvedConfig mirrors the Python CLI's flag set. Keep in sync with
// src/servicectl/cli.py and src/sdk-ts/src/index.ts.
type resolvedConfig struct {
	Name        string `json:"name"`
	Template    string `json:"template"`
	CI          string `json:"ci"`
	Deploy      string `json:"deploy"`
	AzureRegion string `json:"azure_region"`
	Coverage    int    `json:"coverage"`
	Registry    string `json:"registry"`
	OutputDir   string `json:"output_dir"`
	NoGit       bool   `json:"no_git"`
	NoReadme    bool   `json:"no_readme"`
}

// validServiceName mirrors the Python CLI's name validation. Conservative:
// lowercase letters, digits, dash, underscore, dot.
func validServiceName(name string) bool {
	if name == "" {
		return false
	}
	return regexp.MustCompile(`^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$`).MatchString(name)
}

// validTemplate checks the template id against the known set.
func validTemplate(t string) bool {
	switch t {
	case tplNodeExpress, tplNodeReactWeb, tplPythonFlask, tplDotnetWebAPI, tplGoWebAPI:
		return true
	}
	return false
}

// splitName finds the first non-flag argument in rawArgs and moves it to
// the end. This lets the Go flag package parse the remaining flags
// without stopping early at the positional.
//
// Examples (input -> (name, output)):
//
//	[]                                        -> ("", [])
//	["my-svc"]                                -> ("my-svc", [])
//	["my-svc", "--template=foo"]              -> ("my-svc", ["--template=foo"])
//	["--template=foo", "my-svc"]              -> ("my-svc", ["--template=foo"])
//	["--template=foo", "my-svc", "--no-git"]  -> ("my-svc", ["--template=foo", "--no-git"])
//	["--no-git"]                              -> ("", ["--no-git"])   // no positional; flag.Parse handles it
//
// Flags after `--` are left untouched. We don't try to be clever about
// `--foo=bar` vs `--foo bar` because Go's flag package itself accepts both.
func splitName(rawArgs []string) (string, []string) {
	for i, a := range rawArgs {
		if a == "--" {
			// Everything after `--` is positional. Treat the first as the name.
			rest := rawArgs[i+1:]
			if len(rest) == 0 {
				return "", rawArgs[:i]
			}
			return rest[0], append(append([]string{}, rawArgs[:i]...), rest[1:]...)
		}
		if len(a) > 0 && a[0] != '-' {
			// First non-flag arg. Move to end.
			name := a
			others := append(append([]string{}, rawArgs[:i]...), rawArgs[i+1:]...)
			return name, others
		}
	}
	return "", rawArgs
}