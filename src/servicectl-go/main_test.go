package main

import (
	"reflect"
	"testing"
)

func TestSplitName(t *testing.T) {
	cases := []struct {
		name     string
		in       []string
		wantName string
		wantArgs []string
	}{
		{
			name:     "empty",
			in:       []string{},
			wantName: "",
			wantArgs: []string{},
		},
		{
			name:     "name only",
			in:       []string{"my-svc"},
			wantName: "my-svc",
			wantArgs: []string{},
		},
		{
			name:     "name first, then flags",
			in:       []string{"my-svc", "--template=foo"},
			wantName: "my-svc",
			wantArgs: []string{"--template=foo"},
		},
		{
			name:     "flags first, then name",
			in:       []string{"--template=foo", "my-svc"},
			wantName: "my-svc",
			wantArgs: []string{"--template=foo"},
		},
		{
			name:     "flags interleaved around name",
			in:       []string{"--template=foo", "my-svc", "--no-git"},
			wantName: "my-svc",
			wantArgs: []string{"--template=foo", "--no-git"},
		},
		{
			name:     "only flags, no name",
			in:       []string{"--template=foo", "--no-git"},
			wantName: "",
			wantArgs: []string{"--template=foo", "--no-git"},
		},
		{
			name:     "double dash separator",
			in:       []string{"--", "positional-name"},
			wantName: "positional-name",
			wantArgs: []string{},
		},
		{
			name:     "double dash with flags before and name after",
			in:       []string{"--template=foo", "--", "name"},
			wantName: "name",
			wantArgs: []string{"--template=foo"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gotName, gotArgs := splitName(tc.in)
			if gotName != tc.wantName {
				t.Errorf("name = %q, want %q", gotName, tc.wantName)
			}
			if !reflect.DeepEqual(gotArgs, tc.wantArgs) {
				t.Errorf("args = %v, want %v", gotArgs, tc.wantArgs)
			}
		})
	}
}

func TestValidServiceName(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		// Valid.
		{"my-svc", true},
		{"billing-api", true},
		{"my.cool.api", true},
		{"a", true},
		{"svc.v1", true},
		{"my_svc", true},
		{"abc123", true},
		{"a1b2c3", true},

		// Invalid.
		{"", false},
		{"BillingAPI", false},
		{"-leading-dash", false},
		{"trailing-dash-", false},
		{"bad/name", false},
		{"with space", false},
		{"dollar$sign", false},
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := validServiceName(tc.in); got != tc.want {
				t.Errorf("validServiceName(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}

func TestValidTemplate(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"node-express", true},
		{"node-react-web", true},
		{"python-flask", true},
		{"dotnet-webapi", true},
		{"go-webapi", true},
		{"bogus", false},
		{"", false},
		{"NODE-EXPRESS", false}, // case-sensitive on purpose
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := validTemplate(tc.in); got != tc.want {
				t.Errorf("validTemplate(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}

// TestBuildCliArgs pins the contract that buildCliArgs always appends
// `--no-input` LAST in the argv. This is the regression test for the
// 2026-10-12 Windows CI failure where servicectl-go.exe on windows-latest
// (Go 1.22 and Go 1.27) failed at the "Smoke test against the Python CLI"
// step with `aborted.` -- because the Python CLI's interactive prompt
// fired on the inherited stdin (which reports isatty() == True on
// Windows even when no input is available) and then EOF-aborted.
//
// Without `--no-input`, servicectl-go is a programmatic invoker: it has
// no way to answer prompts. Always passing `--no-input` makes its
// intent explicit and works on every platform.
//
// See PR #123 (the prompt feature) and the follow-up fix in this
// commit for the long story.
func TestBuildCliArgs(t *testing.T) {
	cases := []struct {
		name     string
		cfg      resolvedConfig
		wantArgs []string
	}{
		{
			name: "minimal",
			cfg: resolvedConfig{
				Name:         "my-svc",
				Template:     "go-webapi",
				CI:           "github-actions",
				Deploy:       "local",
				AzureRegion:  "eastus",
				Coverage:     80,
				Registry:     "ghcr",
				OutputDir:    ".",
				NoGit:        false,
				NoReadme:     false,
			},
			wantArgs: []string{
				"-m", "servicectl", "init", "my-svc",
				"--template=go-webapi",
				"--ci=github-actions",
				"--deploy=local",
				"--azure-region=eastus",
				"--coverage=80",
				"--registry=ghcr",
				"--output-dir=.",
				"--no-input", // ALWAYS LAST
			},
		},
		{
			name: "with no-git and no-readme",
			cfg: resolvedConfig{
				Name:         "smoke-svc",
				Template:     "go-webapi",
				CI:           "github-actions",
				Deploy:       "local",
				AzureRegion:  "eastus",
				Coverage:     80,
				Registry:     "ghcr",
				OutputDir:    "/tmp/smoke",
				NoGit:        true,
				NoReadme:     true,
			},
			wantArgs: []string{
				"-m", "servicectl", "init", "smoke-svc",
				"--template=go-webapi",
				"--ci=github-actions",
				"--deploy=local",
				"--azure-region=eastus",
				"--coverage=80",
				"--registry=ghcr",
				"--output-dir=/tmp/smoke",
				"--no-git",
				"--no-readme",
				"--no-input", // ALWAYS LAST
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := buildCliArgs(tc.cfg)
			if !reflect.DeepEqual(got, tc.wantArgs) {
				t.Errorf("buildCliArgs() = %v\nwant     %v", got, tc.wantArgs)
			}
			// Defensive: the LAST element must be --no-input regardless
			// of any other config variations. If this ever changes,
			// update the test deliberately -- don't just shuffle.
			if got[len(got)-1] != "--no-input" {
				t.Errorf("buildCliArgs() last arg = %q, want --no-input", got[len(got)-1])
			}
		})
	}
}