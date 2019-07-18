# Copyright 2018 Delft University of Technology
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Modelsim/Questasim target for vhdeps. Generates a TCL script that will
simulate all toplevel entities ending in _tc (or only those specified on the
command line) as a regression test in either batch mode or GUI mode depending
on how the simulator is started. Test cases are expected to terminate by
running out of events in case of success and with a "severity failure" report
statement or simulator timeout in case of failure. This is reflected in the
exit status of vsim when running in batch mode."""

import tempfile
import os
import re
from .shared import add_arguments_for_get_test_cases, get_test_cases, run_cmd

_HEADER = """\
# Copyright 2018 Delft University of Technology
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


#------------------------------------------------------------------------------
# Global variables
#------------------------------------------------------------------------------

# List of all non-standard library names in use by the project.
quietly set libs [list]

# Absolute path to the directory that we compile the libraries into.
quietly set libdir [pwd]

# Whether the modelsim.ini in a test case working directory was created by us
# (and thus should be cleaned up) or was created by the user.
quietly set del_modelsim_ini false

# List of all source files. Every entry is a dictionary with the following
# keys:
#  - fname: the source filename.
#  - lib: the name of the library it should be compiled into.
#  - flags: any flags that should be passed to vcom.
#  - stamp: timestamp when this file was last compiled, or 0 if it hasn't been
#    compiled yet. This is matched with the file's mtime when looking for|
#    changes since the previous compilation.
quietly set sources [list]

# List of all test cases. Every entry is a dictionary with the following keys:
#  - lib: the library containing the test case.
#  - unit: the name of the test case entity.
#  - workdir: the directory that the test case should run relative to, such
#    that any VHDL file I/O relative paths are resolved correctly.
#  - timeout: timeout for the test case.
#  - flags: any flags that should be passed to vsim.
#  - suppress_warnings: whether numeric_std warnings and the likes should be
#    suppressed.
#  - log_all: whether all signals in the entire system should be logged by
#    default. This is convenient when debugging because you don't have to rerun
#    every time you add a signal, but can be costly for large tests.
#  - wave_config: location of a TCL script that is run before and after the
#    test case to set up the waveform viewer, or empty if the default waveform
#    config should be used. The script is run before the test to ensure that
#    all signals needed have been added properly and thus will be logged, and
#    after the test such that the waveform zoom level and position is correct
#    in case the script sets this.
#  - result: the result of the test case. One of "unknown", "passed",
#    "timeout", "failed", or "error".
#  - result_valid: whether the aforementioned result is up-to-date. Any
#    recompilation where any files changed clears this flag.
quietly set test_cases [list]

# The index in test_cases of the currently open test, or -1 if none.
quietly set current_test -1

# The index of the previously run test case.
quietly set rerun_test -1


#------------------------------------------------------------------------------
# Helper commands, not intended to be called by the user interactively
#------------------------------------------------------------------------------

# Adds a source to the source list.
proc add_source {fname lib flags} {
  global sources libs
  close_sim

  # Make sure the file exists.
  if {![file exists $fname]} {
    error "$fname does not exist."
    return
  }

  # Make sure the library exists; create it if it doesn't yet.
  if {[lsearch $libs $lib] == -1} {
    vlib $lib
    lappend libs $lib
  }

  # If the file is already in the compile order, just set its flags and return.
  foreach source $sources {
    if {[dict get $source fname] == $fname} {
      set flags new_flags
      return
    }
  }

  # Add the file.
  lappend sources [dict create  \\
                   fname $fname \\
                   lib   $lib   \\
                   flags $flags \\
                   stamp 0]
}

# Adds a test case to the test case list. Returns its index/ID.
proc add_test {lib unit workdir timeout flags suppress_warnings log_all wave_config} {
  global test_cases
  set test_case [dict create                \\
    lib                 $lib                \\
    unit                $unit               \\
    workdir             $workdir            \\
    timeout             $timeout            \\
    flags               $flags              \\
    suppress_warnings   $suppress_warnings  \\
    log_all             $log_all            \\
    wave_config         $wave_config        \\
    result              "unknown"           \\
    result_valid        false               \\
  ]

  lappend test_cases $test_case
  return [expr {[llength $test_cases] - 1}]
}

# Adds signals matching a given pattern to a given group in the wave viewer,
# coloring the signals based on their role in the simulation.
proc add_signals_to_wave {
  group pattern
  {in_color #00FFFF}
  {out_color #FFFF00}
  {internal_color #FFFFFF}
} {
  catch {add wave -noupdate -expand -group $group $pattern}

  set input_list    [lsort [find signals -in       $pattern]]
  set output_list   [lsort [find signals -out      $pattern]]
  set internal_list [lsort [find signals -internal $pattern]]

  proc colorize {vhd_list color} {
    foreach obj $vhd_list {
      # get leaf name
      set nam [lindex [split $obj /] end]
      # change color
      property wave $nam -color $color
    }
  }

  colorize $input_list    $in_color
  colorize $output_list   $out_color
  colorize $internal_list $internal_color

  WaveCollapseAll 0
}

# Run the given test case with the given integer index/ID.
proc run_test_by_id {index {run_to {}} {fast {}}} {
  global libdir libs del_modelsim_ini test_cases current_test rerun_test
  global StdArithNoWarnings StdNumNoWarnings NumericStdNoWarnings

  if {$fast == "-fast"} {
    set fast true
  } elseif {$fast == {}} {
    set fast false
  } else {
    echo "flag must be -fast or nothing"
    return
  }

  # Close any currently running simulation before starting a new one. This
  # also cleans up the previous working directory if it wasn't the library
  # dir, and cd's to $libdir. It might also mutate $test_cases, so we do this
  # outside the dict with command.
  close_sim

  set test_case [lindex $test_cases $index]
  dict with test_case {

    # When we change to the working directory for the test case, modelsim will
    # create or modify a modelsim.ini file. If modelsim creates it, we'd like
    # to clean it up once we're done to avoid littering.
    set del_modelsim_ini [expr ![file exists $workdir/modelsim.ini]]

    # Write a file for vhdeps indicating which files must still be cleaned up.
    # vhdeps can then clean them up if modelsim terminates before close_sim is
    # called.
    set outfile [open ".cleanup" w]
    if {$del_modelsim_ini} {
      puts $outfile $workdir/modelsim.ini
    }
    puts $outfile $workdir/vsim.wlf
    close $outfile

    # Change to the working directory for this test case. If that was no-op,
    # just return.
    cd $workdir

    # Map the libraries to the ones in $libdir so we can access them.
    foreach lib $libs {
      vmap $lib $libdir/$lib
    }

    # Give the command to initialize the simulation.
    eval "vsim $flags $lib.$unit"

    # Enable or disable library warnings based on preferences.
    set StdArithNoWarnings $suppress_warnings
    set StdNumNoWarnings $suppress_warnings
    set NumericStdNoWarnings $suppress_warnings

    # Add signals to the waveform if we're not running in batch mode.
    if {![batch_mode] && !$fast} {

      # Add all signals to the log if requested.
      if {$log_all} {
        catch {add log -recursive *}
      }

      # If we've run this test case before or the user specified a waveform
      # configuration file for the test case manually, load that configuration
      # instead of setting up the defaults.
      if {[file exists $wave_config]} {
        do $wave_config
      } else {
        configure wave -signalnamewidth 1
        add_signals_to_wave {Toplevel} sim:/[string tolower $unit]/*
        foreach instance [find instances sim:/[string tolower $unit]/*] {
          set instance_path [lindex $instance 0]
          set instance_name [lindex [split $instance_path /] 2]
          add_signals_to_wave $instance_name $instance_path/*
        }
        configure wave -namecolwidth 256
        configure wave -valuecolwidth 192
      }

    }

    # Run until either the test case terminates using a failure report, by
    # event starvation, or due to a timeout. Failure causes a break, which we
    # don't want killing this script, so we have to wrap it in onbreak resume.
    onbreak resume
    if {$run_to != {}} {
      run $run_to
    } else {
      run $timeout
    }
    onbreak ""

    # To detect which of the three things occurred, we need to do some arcane
    # stuff, because unfortunately there doesn't seem to be a direct query for
    # it in modelsim. Note that $result is part of the test case dictionary, so
    # the dict implicitely gets updated as well.
    set status1 [runStatus -full]
    onbreak resume
    run -step
    onbreak ""
    set status2 [runStatus -full]
    echo $status1 $status2
    if {$status2 eq "ready end"} {
      set result passed
    } elseif {$status1 eq "ready end"} {
      set result timeout
    } else {
      set result failed
    }
    set result_valid true

    # Clean up the GUI.
    if {![batch_mode]} {

      # The run -step command (and possibly the run $timeout command) will have
      # opened and focused a text editor for whatever VHDL source line was last
      # executed. We want to pull the waveform view forward instead, since that
      # actually contains relevant information.
      foreach window [view] {
        if {[string first ".wave" $window] != -1} {
          view $window
          break
        }
      }

      # If we have a waveform configuration file, run it again to also restore
      # stuff like horizontal position and zoom. We have to run it twice to
      # both ensure that all the signals viewed by the user are logged and that
      # the zoom level is correct. If we don't have a file, just zoom full.
      if {!$fast} {
        if {[file exists $wave_config]} {
          delete wave *
          do $wave_config
        } else {

          # It would be nice to zoom in on the part of the test case that's
          # actually doing something versus the entire timeout period, but
          # unfortunately there doesn't seem to be a way to detect when this is.
          # Things that have been attempted:
          #  - Querying the various simulation status commands and (documented)
          #    variables for timestamps. These are invariably equal to $timeout.
          #  - Using a cursor; the idea was to make one, set it to the timeout
          #    time, and then move it backward to the previous event. But there's
          #    no such command.
          #  - Using run -all in conjunction with the after command to check the
          #    current simulation time every few milliseconds and asynchronously
          #    stop it once the timeout is reached. Unfortunately, this locks up
          #    the modelsim GUI.
          #  - Calling run <timestep> inside a loop, heuristically determining
          #    the timestep to balance simulation kernel time with TCL execution
          #    time. This has multiple problems: A) in order to detect whether
          #    the simulation is done a run -step is needed, which opens a text
          #    editor with breakpoint etc and is therefore slow; B) the time
          #    command does not work on run for some reason, so the CPU time
          #    needs to be determined in some other way; C) if the user presses
          #    the stop button during the simulation this doesn't break out of
          #    the loop.
          # So this will have to make do.
          wave zoom full

        }
      }
    }
  }

  # Update the test_cases global with the updated dict.
  lset test_cases $index $test_case

  # Indicate that we're currently in a simulation environment for the test case
  # we just ran.
  set current_test $index

  # If we're running in fast (test suite) mode, close the simulation
  # immediately and clear the rerun_test flag. Otherwise set rerun_test to this
  # test case.
  if {$fast} {
    set rerun_test -1
    close_sim
  } else {
    set rerun_test $index
  }

  # Return the test case result.
  return $result
}


#------------------------------------------------------------------------------
# Commands that the user may call interactively
#------------------------------------------------------------------------------

# Compile sources added with add_source incrementally. If called with -force,
# all files will be recompiled; otherwise the file timestamps are used.
proc recompile {{force {}}} {
  global sources test_cases
  close_sim

  # Once we have to compile something, all subsequent files are recompiled as
  # well, since they may depend on the changed file. $compile tracks whether
  # we've had to recompile anything yet. It's simply initialized to true when
  # we want to recompile everything.
  if {$force == "-force"} {
    set compile true
  } elseif {$force == {}} {
    set compile false
  } else {
    echo "Invalid arguments, must be -force or nothing."
    return
  }

  set index -1
  foreach source $sources {
    dict with source {

      # If the file has been modified since it was last compiled, set the
      # $compile flag.
      set new_stamp [file mtime $fname]
      if {$new_stamp > $stamp} {
        set compile true
      }

      # Compile the file if we need to.
      if {$compile} {
        echo "Compiling \\(-work $lib $flags\\):" [file tail $fname]
        set stamp $new_stamp
        eval vcom "-quiet -work $lib $flags $fname"
      }
    }
    lset sources [incr index] $source
  }

  # If we had to recompile anything, mark all test case results as out of date.
  if {$compile} {
    for {set index 0} {$index < [llength $test_cases]} {incr index} {
      set test_case [lindex $test_cases $index]
      dict set test_case result_valid false
      lset test_cases $index $test_case
    }
  }
}

# Runs the given test by name, which can include wildcards. The first matching
# test case found in the list of test cases is run. If any source files changed
# since the last compilation, they are recompiled. If no name is specified and
# there is only one test, it is run; otherwise the names of all test cases are
# returned.
proc test {{name {}}} {
  global test_cases

  if {$name != {}} {

    # Get the test case ID for the given name through pattern matching.
    set test_index -1
    for {set index 0} {$index < [llength $test_cases]} {incr index} {
      set test_case [lindex $test_cases $index]
      dict with test_case {
        if {[string match $name "$lib.$unit"] || [string match $name "$unit"]} {
          set test_index $index
          break
        }
      }
    }
    if {$test_index <= -1} {
      for {set index 0} {$index < [llength $test_cases]} {incr index} {
        set test_case [lindex $test_cases $index]
        dict with test_case {
          if {[string match "*$name*" "$lib.$unit"]} {
            set test_index $index
            break
          }
        }
      }
    }
    if {$test_index <= -1} {
      echo "Test case not found."
      return
    }

  } elseif {[llength $test_cases] == 1} {

    # Run the only test case.
    set test_index 0

  } else {

    # Print the names of all test cases and return without simulating.
    echo "Available test cases:"
    foreach test_case $test_cases {
      dict with test_case {
        if {$result == "unknown"} {
          echo " - $lib.$unit"
        } elseif {!$result_valid} {
          echo " - $lib.$unit ($result/out of date)"
        } else {
          echo " - $lib.$unit ($result)"
        }
      }
    }
    return

  }

  # Make sure everything is compiled.
  recompile

  # Run the test case.
  return [run_test_by_id $test_index]
}

# Closes any running simulation, and changes the working directory back to the
# library dir.
proc close_sim {} {
  global libdir del_modelsim_ini current_test rerun_test test_cases

  if {$current_test > -1} {

    # If we were running a test case and are not in batch mode, do some GUI
    # cleanup.
    if {![batch_mode]} {

      # Close any source view windows that might be open. If we don't do this,
      # regression test simulation will slow down polynomially in the GUI,
      # because modelsim keeps trying to rerender the source viewers... which
      # it's not very good at.
      set windows [view]
      foreach window $windows {
        if {[string first ".source" $window] != -1} {
          noview $window
        }
      }

      # Save the waveform state if this test is rerunnable; tests started with
      # -fast are exempted, as their waveform view is never initialized. We
      # always write to a temporary file in our library working directory, so
      # we don't accidentally override the user's script for the initial
      # configuration.
      if {$rerun_test > -1} {
        set test_case [lindex $test_cases $current_test]
        dict with test_case {
          set wave_config ${libdir}/${lib}.${unit}.wave.cfg
          write format wave $wave_config
        }
        lset test_cases $current_test $test_case
      }
    }

    # Indicate that we're no longer simulating a test case.
    set current_test -1

  }

  # Quit any ongoing simulation. Note that the user might have started one
  # outside of our control as well, so we need to do this regardless of the
  # state of $current_test
  quit -sim

  # Change back to the library directory if we're not already there. A false
  # negative here (as in, not detecting that we're in the library directory
  # when in fact we are) is bad, because we incorrectly delete modelsim.ini
  # then.
  set workdir [pwd]
  if {$workdir != $libdir} {
    cd $libdir

    # Modelsim tracks its library mappings (vmap) in modelsim.ini, which it
    # spams in the working directory whenever vmap is run. Such littering is
    # undesirable, so delete it if we created it.
    if {$del_modelsim_ini} {
      file delete $workdir/modelsim.ini
      set del_modelsim_ini false
    }

    # Also delete the waveform data.
    file delete $workdir/vsim.wlf

    # Since we just handled the deletions, we can get rid of the .cleanup file
    # we created for vhdeps in case we wouldn't get here.
    file delete ".cleanup"

  }

}

# This command re-runs the previous or current test case, if any.
proc rerun {} {
  global rerun_test current_test now UserTimeUnit

  # Make sure we have a test to rerun.
  if {$rerun_test <= -1} {
    echo "No simulation to rerun."
    return error
  }

  # The user might have advanced time beyond the test case timeout. If the
  # simulation is still open, fetch the current simulation time and make sure
  # we rerun until that point.
  set run_to {}
  if {$current_test >= -1} {
    set run_to "$now $UserTimeUnit"
  }

  # Make sure everything is compiled.
  recompile

  # Rerun the test case.
  return [run_test_by_id $rerun_test $run_to]
}

# This command runs all test cases. If a filename is passed, the summary is
# saved to that file in addition to being echoed.
proc suite {{outfile {}}} {
  global test_cases

  # Make sure everything is compiled.
  recompile

  # Run all test cases that are out of date or haven't been simulated yet.
  for {set index 0} {$index < [llength $test_cases]} {incr index} {
    set test_case [lindex $test_cases $index]
    dict with test_case {
      if {$result == "unknown" || !$result_valid} {
        run_test_by_id $index {} -fast
      }
    }
  }

  # Print the test result summary.
  return [summary $outfile]
}

# This command intelligently opens the first failing test case. If a previously
# failing test case passes, it runs all out-of-date tests and then calls itself
# again if there are still failures.
proc debug {} {
  global test_cases

  # Look for tests that are known to have failed or timed out.
  set test_index -1
  for {set index 0} {$index < [llength $test_cases]} {incr index} {
    set test_case [lindex $test_cases $index]
    dict with test_case {
      if {$result_valid && $result != "passed" && $result != "unknown"} {
        set test_index $index
      }
    }
  }

  # If we found such a test, run it. If it fails, let the user debug it; if it
  # passes now, continue by (re)running the rest of the suite.
  if {$test_index != -1} {
    recompile
    if {[run_test_by_id $test_index] == "passed"} {
      echo "Test case passes now! Checking the rest of the suite again..."
      after 1500
    } else {
      return "failed"
    }
  }

  # Run the entire suite again. If there are failures still, recursively call
  # ourselves.
  if {[suite] == "passed"} {
    echo "No more failures!"
    return "passed"
  } else {
    return [debug]
  }
}

# Summarizes the test results (so far). If a filename is passed, the summary is
# saved to that file in addition to being echoed.
proc summary {{outfile {}}} {
  global test_cases

  # Order the test cases by importance, such that the most important ones are
  # at the bottom. That way, the user doesn't have to scroll up to see what
  # matters. Test cases that have not been simulated yet or are out of date are
  # more important than up-to-date test cases, and failures are more important
  # than passes.
  proc test_result_weight {test_case} {
    dict with test_case {
      if {$result == "passed"} {
        set weight 0
      } elseif {$result == "timeout"} {
        set weight 2
      } elseif {$result == "failed"} {
        set weight 4
      } elseif {$result == "error"} {
        set weight 6
      } else { # unknown
        set weight 8
      }
      if {!$result_valid} {
        set weight [expr {$weight + 1}]
      }
    }
    return $weight
  }
  proc order_test {left right} {
    set left [test_result_weight $left]
    set right [test_result_weight $right]
    if {$left < $right} {
      return -1
    } elseif {$left > $right} {
      return 1
    } else {
      return 0
    }
  }
  set test_case_order [lsort -command order_test -indices $test_cases]

  # Open the output file, if any.
  if {$outfile != {}} {
    set outfile [open $outfile w]
  }

  # Writes a line to the user and to the given output file channel.
  proc println {outfile line} {
    echo $line
    if {$outfile != {}} {
      puts $outfile $line
    }
  }

  # Print the summary, in a way that's consistent with the other vhdeps
  # targets.
  println $outfile "Summary:"
  set done true
  set passed true
  foreach index $test_case_order {
    set test_case [lindex $test_cases $index]
    dict with test_case {
      if {$result == "unknown"} {
        set line " * ?       $lib.$unit"
        set done false
      } else {
        set line [format " * %-7s %s" [string toupper $result] "$lib.$unit"]
        if {!$result_valid} {
          set line "$line (out of date)"
          set done false
        }
      }
      println $outfile $line
      if {$result != "passed"} {
        set passed false
      }
    }
  }
  if {!$done} {
    println $outfile "Test suite incomplete"
    return "incomplete"
  } elseif {$passed} {
    println $outfile "Test suite PASSED"
    return "passed"
  } else {
    println $outfile "Test suite FAILED"
    return "failed"
  }

  # Close the output file, if any.
  if {$outfile != {}} {
    close $outfile
  }
}

# Lists the custom commands provided by this script with some basic docs.
proc vhdeps_help {} {
  echo "vhdeps commands:"
  echo ""
  echo "recompile     Compiles any sources that may have changed and their"
  echo "              dependants. This is normally called automatically. Add"
  echo "              -force to recompile all sources regardless of timestamp."
  echo "              Note that any changes to the structure of the project may"
  echo "              require vhdeps to be rerun."
  echo ""
  echo "test ?name?   If no name is specified, the names of all known test cases"
  echo "              are printed. Otherwise, the name is treated as a pattern"
  echo "              to select a test case. If there is a matching test case,"
  echo "              it is run and stays open for waveform inspection."
  echo ""
  echo "close_sim     Closes an open simulation, cleaning up any temporary files"
  echo "              left behind. This is normally called automatically."
  echo ""
  echo "rerun         Reruns the currently open or previously opened simulation."
  echo "              If any files changed, they are recompiled. The waveform"
  echo "              configuration is restored to its previous state."
  echo ""
  echo "suite         Runs the entire test suite and prints a summary. Pass a"
  echo "              filename as argument to also write the summary to a file."
  echo ""
  echo "summary       Prints a summary of all current test case results. Pass a"
  echo "              filename as argument to also write the summary to a file."
  echo ""
  echo "debug         Runs the first known-failing test case, recompiling"
  echo "              sources if anything changed. If no test cases are known"
  echo "              to fail, run the suite command to run everything, then"
  echo "              try again."
  echo ""
  echo "vhdeps_help   Prints this help message."
}


#------------------------------------------------------------------------------
# Startup command
#------------------------------------------------------------------------------

proc autorun {} {
  global test_cases

  # Generated code:
"""

_FOOTER = """\
  # End of generated code.

  # If we're running in batch mode, just run everything and return the result
  # through the exit code.
  if {[batch_mode]} {
    if {[suite] != "passed"} {
      exit -code 1
    } else {
      exit -code 0
    }
  }

  # In GUI mode with one test case, initialize that test case and let the user
  # interact with it. If there are multiple test cases, run everything and then
  # let the user decide how to proceed.
  if {[llength $test_cases] == 1} {
    recompile
    run_test_by_id 0
    echo "--------"
    echo {Useful commands: [rerun] to rerun the test, [vhdeps_help] for more info}
  } else {
    if {[suite] != "passed"} {
      debug
      echo "--------"
      echo {Opened the first failing test. Useful commands: [debug] to try again, [vhdeps_help] for more}
    } else {
      echo "--------"
      echo {Useful commands: [debug] to rerun the suite, [test <name>] to rerun/open a specific test, [vhdeps_help] for more}
    }
  }
}

autorun
"""

def add_arguments(parser):
    """Adds the command-line arguments supported by this target to the given
    `argparse.ArgumentParser` object."""
    add_arguments_for_get_test_cases(parser)

    parser.add_argument(
        '--tcl', action='store_true',
        help='Don\'t run vsim; just output the TCL script.')

    parser.add_argument(
        '--gui', action='store_true',
        help='Launch vsim in GUI mode versus batch mode.')

    parser.add_argument(
        '--no-tempdir', action='store_true',
        help='Disables cwd\'ing to a temporary working directory.')

    parser.add_argument(
        '--suppress-warnings', action='store_true',
        help='Suppress warnings generated by numeric_stc, std_arith, and '
        'std_num for all test cases. Can also be done per test case with '
        '"-- pragma vhdeps vsim suppress-warnings" in the test case VHDL '
        'file.')

    parser.add_argument(
        '-W', action='append', metavar='#,<options>', dest='extra_flags',
        help='Pass comma-separated options to the command specified by #, '
        'which can be \'c\' for vcom and \'s\' for vsim.')

def _write_tcl(vhd_list, tcl_file, suppress_warnings, extra_flags, **kwargs):
    """Writes the TCL file for the given VHDL list and testcase pattern to
    `outfile`."""
    tcl_file.write(_HEADER)

    # Parse the -W command line parameters.
    vcom_flags = []
    vsim_flags = ['-novopt']
    if extra_flags:
        for extra_flag in extra_flags:
            if ',' not in extra_flag:
                raise ValueError('invalid value for -W')
            target, *flags = extra_flag.split(',')
            if target == 'c':
                vcom_flags.extend(flags)
            elif target == 's':
                vsim_flags.extend(flags)
            else:
                raise ValueError('invalid value for -W')

    for vhd in vhd_list.order:
        flags = ['-quiet']
        if vhd.version <= 1987:
            flags.append('-87')
        elif vhd.version <= 1993:
            flags.append('-93')
        elif vhd.version <= 2002:
            flags.append('-2002')
        elif vhd.version <= 2008:
            flags.append('-2008')
        else:
            raise ValueError('VHDL version %d is not supported' % vhd.version)

        # Handle vcom pragmas.
        with open(vhd.fname, 'r') as fil:
            contents = fil.read()
        for match in re.finditer(r'\-\-\s*pragma\s+vhdeps\s+vcom\s+([^\n]+)\n', contents):
            pragma = match.group(1)
            if pragma.startswith('flags '):
                flags.append(pragma[6:])

        # Flags specified on the command line take precedence over pragmas.
        flags.extend(vcom_flags)
        flags = ' '.join(flags)

        tcl_file.write('  add_source {%s} {%s} {%s}\n' % (vhd.fname, vhd.lib, flags))

    test_cases = get_test_cases(vhd_list, **kwargs)
    for test_case in test_cases:
        suppress_warnings_tc = suppress_warnings
        log_all = True
        flags = []
        wave_config = ''

        # Handle vsim pragmas.
        with open(test_case.file.fname, 'r') as fil:
            contents = fil.read()
        for match in re.finditer(r'\-\-\s*pragma\s+vhdeps\s+vsim\s+([^\n]+)\n', contents):
            pragma = match.group(1)
            if pragma == 'suppress-warnings':
                suppress_warnings_tc = True
            elif pragma == 'no-log-all':
                log_all = False
            elif pragma.startswith('flags '):
                flags.append(pragma[6:])
            elif pragma.startswith('wave-config-tcl '):
                wave_config = pragma.split(maxsplit=1)[1]

        # Flags specified on the command line take precedence over pragmas.
        flags.extend(vsim_flags)
        flags = ' '.join(flags)

        tcl_file.write('  add_test {%s} {%s} {%s} \\\n    {%s} {%s} %s %s {%s}\n' % (
            test_case.file.lib, test_case.unit, os.path.dirname(test_case.file.fname),
            test_case.file.get_timeout(), flags, suppress_warnings_tc, log_all, wave_config))

    tcl_file.write(_FOOTER)

def _run(vhd_list, output_file, gui=False, **kwargs):
    """Runs this backend in the current working directory."""
    try:
        from plumbum.cmd import vsim
    except ImportError:
        raise ImportError('no vsim-compatible simulator was found.')
    from plumbum import local

    # Write the TCL file to a temporary file.
    with open('vsim.do', 'w') as tcl_file:
        _write_tcl(vhd_list, tcl_file, **kwargs)

    # Run vsim in the requested way.
    if gui:
        cmd = vsim['-do', 'vsim.do']
    else:
        cmd = local['cat']['vsim.do'] | vsim
    exit_code, *_ = run_cmd(output_file, cmd)

    # If the TCL script left us with a .cleanup file, delete the files listed
    # in it.
    if os.path.isfile('.cleanup'):
        with open('.cleanup', 'r') as fildes:
            for fname in fildes:
                try:
                    os.remove(fname.rstrip())
                except OSError:
                    pass
        os.remove('.cleanup')

    # Forward vsim's exit code.
    return exit_code

def run(vhd_list, output_file, tcl=False, no_tempdir=False, **kwargs):
    """Runs this backend."""

    # If we just need to output TCL, short-circuit the rest of the backend.
    if tcl:
        _write_tcl(vhd_list, output_file, **kwargs)
        return 0

    try:
        from plumbum import local
    except ImportError:
        raise ImportError('the vsim backend requires plumbum to be installed '
                          'to run vsim (pip3 install plumbum).')

    if no_tempdir:
        return _run(vhd_list, output_file, **kwargs)
    with tempfile.TemporaryDirectory() as tempdir:
        with local.cwd(tempdir):
            return _run(vhd_list, output_file, **kwargs)
