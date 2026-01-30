import subprocess
import json
import networkx as nx
from rich.console import Console
from rich.tree import Tree
import argparse
import os
import time

console = Console()

# Cache configuration
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".brew_analyzer_cache")
CACHE_FILE = os.path.join(CACHE_DIR, "brew_data.json")
CACHE_EXPIRATION_SECONDS = 3600 # 1 hour

def _execute_brew_command(command_args):
    """
    Executes a brew command and returns the raw stdout.
    Handles CalledProcessError and returns None on failure.
    """
    try:
        result = subprocess.run(
            ["brew"] + command_args,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error executing brew command: {' '.join(command_args)}: {e!r}[/red]")
        console.print(f"[red]Stderr: {e.stderr}[/red]")
        return None
    except Exception as e:
        console.print(f"[red]An unexpected error occurred: {e!r}[/red]")
        return None

def get_brew_info_json(package_name=None, installed_only=False, casks=False):
    """
    Executes 'brew info --json=v2' for a specific package or all installed packages
    and returns the parsed JSON output.
    """
    command_args = ["info", "--json=v2"]
    if casks:
        command_args.append("--casks")
    if installed_only:
        command_args.append("--installed")
    if package_name:
        command_args.append(package_name)

    stdout = _execute_brew_command(command_args)
    if stdout:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            console.print(f"[red]Error decoding JSON from brew output for command {' '.join(command_args)}: {e!r}[/red]")
            return None
    return None

def load_from_cache():
    """
    Loads data from cache if it's valid (not expired).
    Returns cached data or None if cache is invalid/missing.
    """
    if os.path.exists(CACHE_FILE):
        file_mtime = os.path.getmtime(CACHE_FILE)
        if (time.time() - file_mtime) < CACHE_EXPIRATION_SECONDS:
            with open(CACHE_FILE, "r") as f:
                try:
                    console.print("[dim]Loading data from cache...[/dim]")
                    return json.load(f)
                except json.JSONDecodeError:
                    console.print("[yellow]Warning: Cache file corrupted, will refresh.[/yellow]")
        else:
            console.print("[dim]Cache expired, will refresh.[/dim]")
    return None

def save_to_cache(data):
    """
    Saves data to the cache file.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)
    console.print("[dim]Data saved to cache.[/dim]")

def get_all_installed_brew_data(force_refresh=False):
    """
    Fetches JSON data for all installed formulae and casks, using cache if available.
    """
    if not force_refresh:
        cached_data = load_from_cache()
        if cached_data:
            return cached_data

    console.print("[bold green]Refreshing Homebrew data...[/bold green]")
    all_data = {
        "formulae": [],
        "casks": []
    }

    # Fetch formulae data
    formulae_data = get_brew_info_json(installed_only=True)
    if formulae_data and "formulae" in formulae_data:
        all_data["formulae"] = formulae_data["formulae"]
    else:
        console.print("[yellow]Warning: Could not fetch formulae data or it was empty.[/yellow]")

    # Fetch casks data
    casks_data = get_brew_info_json(installed_only=True, casks=True)
    if casks_data and "casks" in casks_data:
        all_data["casks"] = casks_data["casks"]
    else:
        console.print("[yellow]Warning: Could not fetch casks data or it was empty.[/yellow]")

    if all_data["formulae"] or all_data["casks"]:
        save_to_cache(all_data)
    else:
        console.print("[yellow]No data fetched, not saving to cache.[/yellow]")

    return all_data

def build_dependency_graph(all_brew_data):
    """
    Builds a directed graph of Homebrew formulae and cask dependencies.
    Nodes are package names, edges represent 'depends on'.
    """
    graph = nx.DiGraph()

    formulae_data = all_brew_data.get("formulae", [])
    casks_data = all_brew_data.get("casks", [])

    # Create sets of all installed formulae and cask names for efficient lookup
    installed_formulae_names = {f["name"] for f in formulae_data}
    installed_cask_tokens = {c["token"] for c in casks_data}
    
    all_installed_names = installed_formulae_names.union(installed_cask_tokens)

    # Add formulae nodes and their dependencies
    for formula in formulae_data:
        name = formula["name"]
        graph.add_node(name, type="formula", **{k: v for k, v in formula.items() if k != 'name'})

        for dep in formula.get("dependencies", []):
            if dep in installed_formulae_names:
                graph.add_edge(name, dep, type="formula_dep")
        
        for dep in formula.get("build_dependencies", []):
            if dep in installed_formulae_names:
                graph.add_edge(name, dep, type="build_dep")
        
        for opt_dep in formula.get("optional_dependencies", []):
            if opt_dep in installed_formulae_names:
                graph.add_edge(name, opt_dep, type="optional_dep")
    
    # Add casks nodes and their dependencies (specifically to formulae or other casks)
    for cask in casks_data:
        token = cask["token"]
        graph.add_node(token, type="cask", **{k: v for k, v in cask.items() if k != 'token'})

        depends_on = cask.get("depends_on", {})
        if "formula" in depends_on:
            for dep_formula in depends_on["formula"]:
                if dep_formula in installed_formulae_names:
                    graph.add_edge(token, dep_formula, type="cask_to_formula_dep")
        if "cask" in depends_on:
            for dep_cask in depends_on["cask"]:
                # Note: Homebrew cask depends_on logic is complex. 'depends_on: cask' usually refers
                # to requiring another cask to be installed.
                if dep_cask in installed_cask_tokens:
                    graph.add_edge(token, dep_cask, type="cask_to_cask_dep")
    
    return graph


def find_reverse_dependencies(graph, package_name):
    """
    Finds all packages that depend on the given package (reverse dependencies).
    """
    if package_name not in graph:
        return []
    return list(graph.predecessors(package_name))

def find_transitive_dependencies(graph, package_name):
    """
    Finds all packages that the given package depends on, directly or indirectly.
    """
    if package_name not in graph:
        return []
    return list(nx.descendants(graph, package_name))

def find_top_level_packages(graph, all_package_names):
    """
    Identifies packages that have no predecessors in the graph (i.e., nothing installed depends on them).
    These are strong candidates for user-installed packages, not pulled in as dependencies.
    """
    top_level_packages = []
    for pkg_name in all_package_names:
        if pkg_name in graph and not list(graph.predecessors(pkg_name)):
            top_level_packages.append(pkg_name)
    return top_level_packages

def find_explicitly_installed_packages(formulae_data):
    """
    Determines which packages were explicitly installed by the user by checking
    the 'installed_on_request' field.
    Note: This flag is not always reliable for all user-installed packages.
    """
    explicitly_installed = []
    for formula in formulae_data:
        if formula.get("installed_on_request"):
            explicitly_installed.append(formula["name"])
    return explicitly_installed

def print_dependency_tree(graph, package_name, max_depth=None, current_depth=0, parent_tree=None, console_output=True):
    """
    Prints a dependency tree for a given package using rich.Tree.
    """
    if package_name not in graph:
        return

    if parent_tree is None:
        node_type = graph.nodes[package_name].get("type", "unknown")
        color = "bold green" if node_type == "formula" else "bold magenta" if node_type == "cask" else "bold white"
        tree_root = Tree(f"[{color}]{package_name}[/{color}]")
        current_tree = tree_root
    else:
        current_tree = parent_tree

    if max_depth is not None and current_depth >= max_depth:
        return

    for dep in graph.successors(package_name):
        dep_type = graph.nodes[dep].get("type", "unknown")
        dep_color = "green" if dep_type == "formula" else "magenta" if dep_type == "cask" else "white"
        child_node = current_tree.add(f"[{dep_color}]{dep}[/{dep_color}]")
        print_dependency_tree(graph, dep, max_depth, current_depth + 1, child_node, console_output=False) # Pass False to avoid multiple console.print

    if parent_tree is None and console_output: # Only print the root tree once
        console.print(tree_root)

def generate_dot_graph(graph, filename="brew_dependencies.dot", output_image_format=None):
    """
    Generates a DOT graph file from the networkx graph and optionally renders it to an image.
    Requires graphviz to be installed to render to image.
    """
    dot_file_path = filename
    try:
        # Create a copy of the graph to avoid modifying the original
        # and prepare nodes for pydot conversion
        pydot_graph = graph.copy()
        for node in pydot_graph.nodes():
            pydot_graph.nodes[node]["label"] = node
            # Remove 'name' if it somehow sneaked in and conflicts with pydot
            if "name" in pydot_graph.nodes[node]:
                del pydot_graph.nodes[node]["name"]

        dot_string = nx.drawing.nx_pydot.to_pydot(pydot_graph).to_string()
        with open(dot_file_path, "w") as f:
            f.write(dot_string)
        console.print(f"[bold green]DOT graph saved to {dot_file_path}[/bold green]")
        
        if output_image_format:
            image_file_path = f"{os.path.splitext(dot_file_path)[0]}.{output_image_format}"
            try:
                subprocess.run(
                    ["dot", f"-T{output_image_format}", dot_file_path, "-o", image_file_path],
                    check=True,
                    capture_output=True
                )
                console.print(f"[bold green]Image saved to {image_file_path}[/bold green]")
            except FileNotFoundError:
                console.print("[red]Error: 'dot' command not found. Please ensure Graphviz is installed and in your PATH.[/red]")
            except subprocess.CalledProcessError as e:
                console.print(f"[red]Error rendering image with Graphviz: {e!r}[/red]")
                console.print(f"[red]Stderr: {e.stderr.decode()}[/red]")
            except Exception as e:
                console.print(f"[red]An unexpected error occurred during image rendering: {e!r}[/red]")
        else:
            console.print(f"[dim]You can render it to an image using Graphviz (e.g., dot -Tpng {dot_file_path} -o {os.path.basename(dot_file_path)}.png)[/dim]")
        return True
    except ImportError:
        console.print("[red]Error: 'pydot' or 'graphviz' not installed. Cannot generate DOT file.[/red]")
        console.print("[red]Please install them: uv pip install pydot; brew install graphviz[/red]")
        return False
    except Exception as e:
        console.print(f"[red]Error generating DOT graph: {e!r}[/red]")
        return False

def main():
    parser = argparse.ArgumentParser(description="Analyze Homebrew package dependencies.")
    parser.add_argument("package", nargs="?", help="Specific package to analyze (formula or cask). If omitted, lists general overview.")
    parser.add_argument("--format", choices=["summary", "tree", "dot"], default="summary",
                        help="Output format. 'summary' for overview, 'tree' for dependency tree, 'dot' for Graphviz DOT file. (default: summary)")
    parser.add_argument("--depth", type=int, default=3,
                        help="Maximum depth for dependency tree visualization (only for --format tree). (default: 3)")
    parser.add_argument("--output-file", "-o", help="Output file name for DOT graph (only for --format dot). (default: brew_dependencies.dot)")
    parser.add_argument("--image-format", choices=["png", "svg", "jpg"],
                        help="Output image format when --format dot is used. (default: png if --format dot is specified)")
    parser.add_argument("--png", action="store_true", help="Shorthand for --format dot --image-format png")
    parser.add_argument("--svg", action="store_true", help="Shorthand for --format dot --image-format svg")
    parser.add_argument("--jpg", action="store_true", help="Shorthand for --format dot --image-format jpg")
    parser.add_argument("--cask", action="store_true", help="Specify that the package is a cask.")
    parser.add_argument("--refresh-cache", action="store_true", help="Force refresh Homebrew data, ignoring cache.")

    args = parser.parse_args()

    # Handle convenience flags (--png, --svg, --jpg)
    if args.png:
        args.format = "dot"
        args.image_format = "png"
    elif args.svg:
        args.format = "dot"
        args.image_format = "svg"
    elif args.jpg:
        args.format = "dot"
        args.image_format = "jpg"
    # Infer --format dot when --image-format is specified
    elif args.image_format and args.format != "dot":
        args.format = "dot"

    packages_data = get_all_installed_brew_data(force_refresh=args.refresh_cache)

    if not packages_data:
        console.print("[red]Failed to fetch any Homebrew package data. Exiting.[/red]")
        return

    num_formulae = len(packages_data.get('formulae', []))
    num_casks = len(packages_data.get('casks', []))
    console.print(f"[bold green]Successfully fetched data for {num_formulae} formulae and {num_casks} casks.[/bold green]")

    # Build a unified graph for both formulae and casks
    brew_graph = None
    if num_formulae > 0 or num_casks > 0:
        console.print("\n[bold blue]Building unified dependency graph for formulae and casks...[/bold blue]")
        brew_graph = build_dependency_graph(packages_data)
        console.print(f"Graph built with {brew_graph.number_of_nodes()} nodes and {brew_graph.number_of_edges()} edges.")
    else:
        console.print("[yellow]No formulae or casks found to build a dependency graph.[/yellow]")


    if args.package:
        target_package_name = args.package
        target_package_type = "formula" # Assume formula by default

        # Check if it's a cask
        if args.cask or target_package_name in {c["token"] for c in packages_data.get("casks", [])}:
            target_package_type = "cask"

        if brew_graph is None or target_package_name not in brew_graph:
            console.print(f"[{target_package_type.capitalize()} '{target_package_name}' not found in installed packages graph. Exiting.[/red]")
            return

        console.print(f"\n[bold blue]Analyzing '{target_package_name}' ({target_package_type}):[/bold blue]")
        
        if target_package_type == "cask":
            cask_info_data = get_brew_info_json(package_name=target_package_name, casks=True)
            if cask_info_data and cask_info_data["casks"]:
                cask = cask_info_data["casks"][0]

                # Display useful cask information
                name = cask.get("name", [target_package_name])
                if isinstance(name, list) and name:
                    name = name[0]
                console.print(f"  [cyan]Name:[/cyan] {name}")

                if cask.get("desc"):
                    console.print(f"  [cyan]Description:[/cyan] {cask['desc']}")

                # Version and update status
                installed_version = cask.get("installed")
                latest_version = cask.get("version")
                outdated = cask.get("outdated", False)
                if installed_version:
                    version_status = "[red](outdated)[/red]" if outdated else "[green](up to date)[/green]"
                    if installed_version == latest_version:
                        console.print(f"  [cyan]Version:[/cyan] {installed_version} {version_status}")
                    else:
                        console.print(f"  [cyan]Version:[/cyan] {installed_version} → {latest_version} available {version_status}")

                # Extract app name from artifacts
                artifacts = cask.get("artifacts", [])
                for artifact in artifacts:
                    if isinstance(artifact, dict):
                        if "app" in artifact:
                            apps = artifact["app"]
                            if apps:
                                console.print(f"  [cyan]App:[/cyan] {', '.join(apps)}")
                            break

                # Auto-updates
                if cask.get("auto_updates"):
                    console.print(f"  [cyan]Auto-updates:[/cyan] Yes")

                # Homepage
                if cask.get("homepage"):
                    console.print(f"  [cyan]Homepage:[/cyan] {cask['homepage']}")

                # Install date
                install_time = cask.get("installed_time")
                if install_time:
                    from datetime import datetime
                    install_date = datetime.fromtimestamp(install_time).strftime("%Y-%m-%d")
                    console.print(f"  [cyan]Installed:[/cyan] {install_date}")

            # Show graph-based dependencies (e.g., cask depends on a formula)
            direct_deps = list(brew_graph.successors(target_package_name))
            if direct_deps:
                console.print(f"  [cyan]Depends on:[/cyan] {', '.join(direct_deps)}")

            rev_deps = find_reverse_dependencies(brew_graph, target_package_name)
            if rev_deps:
                console.print(f"  [cyan]Required by:[/cyan] {', '.join(rev_deps)}")
            
            if args.format == "tree":
                console.print(f"\n[bold blue]Dependency tree for '{target_package_name}' (max depth {args.depth}):[/bold blue]")
                print_dependency_tree(brew_graph, target_package_name, max_depth=args.depth, console_output=True)
            elif args.format == "dot":
                output_filename = args.output_file if args.output_file else f"{target_package_name}_dependencies.dot"
                subgraph_nodes = [target_package_name] + list(nx.descendants(brew_graph, target_package_name))
                subgraph = brew_graph.subgraph(subgraph_nodes)
                image_format = args.image_format if args.image_format else "png"
                generate_dot_graph(subgraph, output_filename, output_image_format=image_format)
                
        else: # Formula analysis
            # Why was it installed? (Reverse dependencies)
            rev_deps = find_reverse_dependencies(brew_graph, target_package_name)
            if rev_deps:
                console.print(f"  [cyan]Installed because of:[/cyan] {', '.join(rev_deps)}")
                console.print(f"    (These packages depend on '{target_package_name}' to function)")
            else:
                explicitly_installed = find_explicitly_installed_packages(packages_data.get('formulae', []))
                if target_package_name in explicitly_installed:
                    console.print(f"  [cyan]Installed directly by user (flagged 'installed_on_request').[/cyan]")
                else:
                    console.print(f"  [cyan]Installed directly by user (it's a top-level package with no installed dependents).[/cyan]")


            # Direct dependencies
            direct_deps = list(brew_graph.successors(target_package_name))
            console.print(f"  [cyan]Directly depends on:[/cyan] {', '.join(direct_deps) if direct_deps else 'None'}")

            # Transitive dependencies
            trans_deps = find_transitive_dependencies(brew_graph, target_package_name)
            console.print(f"  [cyan]Also depends on (transitive):[/cyan] {', '.join(trans_deps) if trans_deps else 'None'}")

            if args.format == "tree":
                console.print(f"\n[bold blue]Dependency tree for '{target_package_name}' (max depth {args.depth}):[/bold blue]")
                print_dependency_tree(brew_graph, target_package_name, max_depth=args.depth, console_output=True)
            elif args.format == "dot":
                output_filename = args.output_file if args.output_file else f"{target_package_name}_dependencies.dot"
                subgraph_nodes = [target_package_name] + list(nx.descendants(brew_graph, target_package_name))
                subgraph = brew_graph.subgraph(subgraph_nodes)
                image_format = args.image_format if args.image_format else "png"
                generate_dot_graph(subgraph, output_filename, output_image_format=image_format)
        
    else: # No specific package requested
        if args.format == "dot":
            if brew_graph:
                output_filename = args.output_file if args.output_file else "all_brew_dependencies.dot"
                image_format = args.image_format if args.image_format else "png"
                generate_dot_graph(brew_graph, output_filename, output_image_format=image_format)
            else:
                console.print("[yellow]No graph to generate for DOT format.[/yellow]")
        else: # Print general overview
            all_formulae_names = {f["name"] for f in packages_data.get('formulae', [])}
            all_package_names = all_formulae_names.union({c["token"] for c in packages_data.get("casks", [])})

            if num_formulae > 0:
                explicitly_installed_formulae = find_explicitly_installed_packages(packages_data.get('formulae', []))
                
                console.print(f"\n[bold green]Formulae you might have explicitly installed:[/bold green]")
                top_level_formulae = find_top_level_packages(brew_graph, all_formulae_names)
                if top_level_formulae:
                    console.print(f"  [yellow]Top-level packages (no other installed packages depend on these):[/yellow] {', '.join(sorted(top_level_formulae))}")
                    console.print("  [dim]These are strong candidates for packages you installed directly, not as dependencies.[/dim]")
                else:
                    console.print("  [yellow]None found (all formulae seem to be dependencies of other installed packages).[/yellow]")

                if explicitly_installed_formulae:
                    console.print(f"  [yellow]'installed_on_request' flagged packages:[/yellow] {', '.join(sorted(explicitly_installed_formulae))}")
                    console.print("  [dim]This flag indicates they were installed via 'brew install' explicitly.[/dim]")
                else:
                    console.print("  [yellow]No formulae found with 'installed_on_request' flag.[/yellow]")

            if packages_data.get('casks'):
                console.print("\n[bold blue]Casks:[/bold blue]")
                top_level_casks = find_top_level_packages(brew_graph, {c["token"] for c in packages_data["casks"]})
                if top_level_casks:
                    console.print(f"  [yellow]Top-level casks (no other installed packages depend on these):[/yellow] {', '.join(sorted(top_level_casks))}")
                    console.print("  [dim]These are strong candidates for casks you installed directly.[/dim]")
                else:
                    console.print("  [yellow]None found (all casks seem to be dependencies of other installed packages).[/yellow]")
                
                for i, cask in enumerate(packages_data['casks']): # Iterate all casks, not just first 5
                    console.print(f"- {cask['token']} (version: {cask['version']})")
                    if "depends_on" in cask and cask["depends_on"]:
                        console.print(f"  [magenta]Homebrew 'depends_on':[/magenta] {cask['depends_on']}")

if __name__ == "__main__":
    main()