package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// makeFixture creates a temp directory with the given files and returns the path.
// Each entry in files is a relative path; content "" means create empty.
func makeFixture(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, body := range files {
		full := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}
	return root
}

func TestValidate_MinimalLocalService(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile":                "FROM scratch AS build\nFROM alpine\n",
		".gitignore":                "node_modules/\n",
		".gitleaks.toml":            "[allowlist]\n",
		".env.example":              "PORT=8080\n",
		"README.md":                 "# svc\n",
		".github/workflows/ci.yml":  "name: ci\n",
	})

	rep := Validate(root)
	if rep.DeployTarget != "local" {
		t.Errorf("deploy_target = %q, want local", rep.DeployTarget)
	}
	if rep.Summary.Errors != 0 {
		t.Errorf("expected 0 errors, got %d (checks: %+v)", rep.Summary.Errors, rep.Checks)
	}
	if rep.ExitCode != ExitOK {
		t.Errorf("exit_code = %d, want %d", rep.ExitCode, ExitOK)
	}
}

func TestValidate_AzureServiceWithInfra(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile":                 "FROM scratch AS build\nFROM alpine\n",
		".gitignore":                 "*.pyc\n",
		".gitleaks.toml":             "[allowlist]\n",
		".env.example":               "PORT=8080\n",
		"README.md":                  "# svc\n",
		"infra/main.bicep":           "param location string = 'eastus'\n",
		"infra/dev.bicepparam":       "env = 'dev'\n",
		"infra/staging.bicepparam":   "env = 'staging'\n",
		"infra/prod.bicepparam":      "env = 'prod'\n",
		".github/workflows/ci.yml":   "name: ci\n",
	})

	rep := Validate(root)
	if rep.DeployTarget != "azure" {
		t.Errorf("deploy_target = %q, want azure", rep.DeployTarget)
	}
	if rep.Summary.Errors != 0 {
		t.Errorf("expected 0 errors, got %d", rep.Summary.Errors)
	}
	// Multi-stage should be detected.
	for _, c := range rep.Checks {
		if c.Name == "dockerfile:multi-stage" && !c.Passed {
			t.Errorf("multi-stage check should pass with 2 FROM stages")
		}
	}
}

func TestValidate_MissingRequiredFiles(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile": "FROM scratch\n",
		// intentionally missing: .gitignore, .gitleaks.toml, .env.example, README.md
	})

	rep := Validate(root)
	if rep.Summary.Errors < 4 {
		t.Errorf("expected at least 4 errors from missing files, got %d", rep.Summary.Errors)
	}
	if rep.ExitCode != ExitError {
		t.Errorf("exit_code = %d, want %d", rep.ExitCode, ExitError)
	}
}

func TestValidate_DockerfileMultiStage(t *testing.T) {
	cases := []struct {
		name    string
		docker  string
		passed  bool
		message string
	}{
		{
			name:   "two-stage",
			docker: "FROM golang:1.22 AS build\nCOPY . /src\nRUN go build\nFROM alpine\nCOPY --from=build /bin /bin\n",
			passed: true,
		},
		{
			name:   "three-stage",
			docker: "FROM node:20 AS deps\nFROM node:20 AS build\nFROM nginx:alpine\n",
			passed: true,
		},
		{
			name:   "single-stage",
			docker: "FROM node:20\nCOPY . /app\nCMD node app.js\n",
			passed: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := makeFixture(t, map[string]string{
				"Dockerfile": tc.docker,
			})
			rep := Validate(root)
			for _, c := range rep.Checks {
				if c.Name == "dockerfile:multi-stage" {
					if c.Passed != tc.passed {
						t.Errorf("multi-stage passed = %v, want %v (msg=%q)", c.Passed, tc.passed, c.Message)
					}
					return
				}
			}
			t.Fatalf("multi-stage check not found")
		})
	}
}

func TestValidate_PlaintextSecretDetection(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile": "FROM scratch\n",
		".env":       "DATABASE_PASSWORD=hunter2\n",
	})

	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "secrets:env-file" {
			if c.Passed {
				t.Errorf("expected plaintext secret check to FAIL, got pass: %+v", c)
			}
			if c.Severity != SeverityError {
				t.Errorf("expected severity=error, got %q", c.Severity)
			}
			return
		}
	}
	t.Fatalf("secrets:env-file check not found")
}

func TestReport_JSONShape(t *testing.T) {
	root := makeFixture(t, map[string]string{
		"Dockerfile":     "FROM scratch\n",
		".gitignore":     "x\n",
		".gitleaks.toml": "x\n",
		".env.example":   "x\n",
		"README.md":      "x\n",
	})

	rep := Validate(root)
	data, err := json.Marshal(rep)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	s := string(data)
	for _, want := range []string{`"path"`, `"deploy_target"`, `"exit_code"`, `"summary"`, `"checks"`} {
		if !strings.Contains(s, want) {
			t.Errorf("JSON missing field %s in %s", want, s)
		}
	}
}

func TestSummarize(t *testing.T) {
	checks := []Check{
		{Name: "a", Severity: SeverityError, Passed: true},
		{Name: "b", Severity: SeverityError, Passed: false},
		{Name: "c", Severity: SeverityWarn, Passed: false},
		{Name: "d", Severity: SeverityWarn, Passed: true},
		{Name: "e", Severity: SeverityInfo, Passed: false},
	}
	s := summarize(checks)
	if s.Passed != 2 {
		t.Errorf("passed = %d, want 2", s.Passed)
	}
	if s.Errors != 1 {
		t.Errorf("errors = %d, want 1", s.Errors)
	}
	if s.Warnings != 1 {
		t.Errorf("warnings = %d, want 1", s.Warnings)
	}
	if s.Info != 1 {
		t.Errorf("info = %d, want 1", s.Info)
	}
}

func TestComputeExitCode(t *testing.T) {
	cases := []struct {
		name string
		sum  Summary
		want int
	}{
		{"clean", Summary{Passed: 5}, ExitOK},
		{"warnings only", Summary{Passed: 4, Warnings: 1}, ExitWarn},
		{"errors", Summary{Passed: 3, Errors: 1}, ExitError},
		{"both", Summary{Passed: 2, Warnings: 1, Errors: 1}, ExitError},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := Report{Summary: tc.sum}
			if got := r.computeExitCode(); got != tc.want {
				t.Errorf("exit_code = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestInferDeployTarget(t *testing.T) {
	cases := []struct {
		name string
		files map[string]string
		want string
	}{
		{"azure", map[string]string{"infra/main.bicep": "x", "Dockerfile": "x"}, "azure"},
		{"local", map[string]string{"Dockerfile": "x"}, "local"},
		{"unknown", map[string]string{}, "unknown"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := makeFixture(t, tc.files)
			if got := inferDeployTarget(root); got != tc.want {
				t.Errorf("inferDeployTarget = %q, want %q", got, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Go-specific checks (mirror of servicectl.doctor._go_checks).
// ---------------------------------------------------------------------------

// minimalGoService creates a temp dir shaped like a well-formed go-webapi
// scaffold: multi-stage Dockerfile, go.mod with non-placeholder module path,
// cmd/server/main.go, internal/<service>/ with server.go + server_test.go,
// CI workflow with -race.
func minimalGoService(t *testing.T) string {
	t.Helper()
	return makeFixture(t, map[string]string{
		"Dockerfile": "FROM golang:1.22-bookworm AS build\nFROM gcr.io/distroless/static-debian12:nonroot\n",
		"go.mod":     "module example.com/awesome-service\n\ngo 1.22\n",
		"cmd/server/main.go":                   "package main\n",
		"internal/awesome-service/server.go":   "package awesome_service\n",
		"internal/awesome-service/server_test.go": "package awesome_service\n",
		".github/workflows/ci.yml": "- run: go test ./... -race -coverprofile=coverage.out\n",
	})
}

func TestGoChecks_OnlyRunWhenGoSignalsPresent(t *testing.T) {
	// A non-Go service (no go.mod, no cmd/server/main.go) should produce
	// zero `go:*` checks.
	root := makeFixture(t, map[string]string{
		"Dockerfile": "FROM scratch\n",
	})
	rep := Validate(root)
	for _, c := range rep.Checks {
		if strings.HasPrefix(c.Name, "go:") {
			t.Errorf("unexpected Go check on non-Go service: %s", c.Name)
		}
	}
}

func TestGoChecks_WellFormedServicePasses(t *testing.T) {
	root := minimalGoService(t)
	rep := Validate(root)

	expected := []string{
		"go:cmd-server-exists",
		"go:modfile",
		"go:modfile-go-version",
		"go:modfile-module-path",
		"go:has-internal-package",
		"go:has-tests",
		"go:ci-uses-race",
		"go:no-vendor-dir",
	}
	for _, want := range expected {
		found := false
		for _, c := range rep.Checks {
			if c.Name == want {
				found = true
				if !c.Passed {
					t.Errorf("check %s should pass on a well-formed Go service: %s", c.Name, c.Message)
				}
				break
			}
		}
		if !found {
			t.Errorf("missing Go check: %s", want)
		}
	}
}

func TestGoChecks_ModfileMissingIsError(t *testing.T) {
	// cmd/server/main.go is present but go.mod is missing.
	root := makeFixture(t, map[string]string{
		"cmd/server/main.go": "package main\n",
	})
	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "go:modfile" {
			if c.Passed {
				t.Errorf("go:modfile should fail when go.mod is missing")
			}
			if c.Severity != SeverityError {
				t.Errorf("go:modfile severity = %q, want error", c.Severity)
			}
			return
		}
	}
	t.Fatalf("go:modfile check not found")
}

func TestGoChecks_OldGoVersionIsWarning(t *testing.T) {
	root := minimalGoService(t)
	// Bump go.mod down to 1.20.
	if err := os.WriteFile(
		filepath.Join(root, "go.mod"),
		[]byte("module example.com/awesome-service\n\ngo 1.20\n"),
		0o644,
	); err != nil {
		t.Fatalf("rewrite go.mod: %v", err)
	}
	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "go:modfile-go-version" {
			if c.Passed {
				t.Errorf("go:modfile-go-version should fail for go 1.20")
			}
			if c.Severity != SeverityWarn {
				t.Errorf("go:modfile-go-version severity = %q, want warn", c.Severity)
			}
			if !strings.Contains(c.Message, "go 1.20") {
				t.Errorf("expected message to mention 'go 1.20', got %q", c.Message)
			}
			return
		}
	}
	t.Fatalf("go:modfile-go-version check not found")
}

func TestGoChecks_PlaceholderModulePathIsInfoFailure(t *testing.T) {
	root := minimalGoService(t)
	if err := os.WriteFile(
		filepath.Join(root, "go.mod"),
		[]byte("module github.com/henryorsborn/awesome-service\n\ngo 1.22\n"),
		0o644,
	); err != nil {
		t.Fatalf("rewrite go.mod: %v", err)
	}
	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "go:modfile-module-path" {
			if c.Passed {
				t.Errorf("go:modfile-module-path should fail when module still has the henryorsborn placeholder")
			}
			if c.Severity != SeverityInfo {
				t.Errorf("go:modfile-module-path severity = %q, want info", c.Severity)
			}
			return
		}
	}
	t.Fatalf("go:modfile-module-path check not found")
}

func TestGoChecks_NoInternalPackageIsWarning(t *testing.T) {
	root := minimalGoService(t)
	// Remove the populated internal package; leave an empty internal/ directory.
	if err := os.RemoveAll(filepath.Join(root, "internal", "awesome-service")); err != nil {
		t.Fatalf("remove internal pkg: %v", err)
	}
	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "go:has-internal-package" {
			if c.Passed {
				t.Errorf("go:has-internal-package should fail when internal/ has no .go files")
			}
			if c.Severity != SeverityWarn {
				t.Errorf("go:has-internal-package severity = %q, want warn", c.Severity)
			}
			return
		}
	}
	t.Fatalf("go:has-internal-package check not found")
}

func TestGoChecks_NoTestsIsWarning(t *testing.T) {
	root := minimalGoService(t)
	if err := os.Remove(filepath.Join(root, "internal", "awesome-service", "server_test.go")); err != nil {
		t.Fatalf("remove test file: %v", err)
	}
	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "go:has-tests" {
			if c.Passed {
				t.Errorf("go:has-tests should fail when no _test.go files exist")
			}
			if c.Severity != SeverityWarn {
				t.Errorf("go:has-tests severity = %q, want warn", c.Severity)
			}
			return
		}
	}
	t.Fatalf("go:has-tests check not found")
}

func TestGoChecks_NoRaceInCI(t *testing.T) {
	root := minimalGoService(t)
	if err := os.WriteFile(
		filepath.Join(root, ".github", "workflows", "ci.yml"),
		[]byte("- run: go test ./...\n"),
		0o644,
	); err != nil {
		t.Fatalf("rewrite ci.yml: %v", err)
	}
	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "go:ci-uses-race" {
			if c.Passed {
				t.Errorf("go:ci-uses-race should fail when -race is missing from go test")
			}
			if c.Severity != SeverityInfo {
				t.Errorf("go:ci-uses-race severity = %q, want info", c.Severity)
			}
			return
		}
	}
	t.Fatalf("go:ci-uses-race check not found")
}

func TestGoChecks_VendorDirIsInfoFailure(t *testing.T) {
	root := minimalGoService(t)
	if err := os.MkdirAll(filepath.Join(root, "vendor"), 0o755); err != nil {
		t.Fatalf("mkdir vendor: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "vendor", "modules.txt"), []byte("# vendored\n"), 0o644); err != nil {
		t.Fatalf("write vendor/modules.txt: %v", err)
	}
	rep := Validate(root)
	for _, c := range rep.Checks {
		if c.Name == "go:no-vendor-dir" {
			if c.Passed {
				t.Errorf("go:no-vendor-dir should fail when vendor/ exists")
			}
			if c.Severity != SeverityInfo {
				t.Errorf("go:no-vendor-dir severity = %q, want info", c.Severity)
			}
			return
		}
	}
	t.Fatalf("go:no-vendor-dir check not found")
}

func TestParseGoMinor(t *testing.T) {
	cases := []struct {
		in   string
		want int
	}{
		{"1.22", 22},
		{"1.22.0", 22},
		{"1.23.4", 23},
		{"garbage", 0},
		{"", 0},
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := parseGoMinor(tc.in); got != tc.want {
				t.Errorf("parseGoMinor(%q) = %d, want %d", tc.in, got, tc.want)
			}
		})
	}
}
