cmake_minimum_required(VERSION 3.16)
project(ecu_firmware LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# Core library: protocol codec, plant model, config, agent state machine.
add_library(ecu_core STATIC
  src/protocol.cpp
  src/physics.cpp
  src/config.cpp
  src/agent.cpp
)
target_include_directories(ecu_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_executable(ecu_agent src/main.cpp)
target_link_libraries(ecu_agent PRIVATE ecu_core)

# Tests: one self-contained binary, registered with CTest per test name.
enable_testing()
add_executable(ecu_tests tests/test_main.cpp)
target_link_libraries(ecu_tests PRIVATE ecu_core)
set(ECU_TESTS
  crc32
  header_layout
  roundtrip_all_messages
  fault_names
  fault_spec_parsing
  config_parsing
  physics_determinism
  physics_behaviour
  overtemp_injection
  other_injections
)
foreach(t IN LISTS ECU_TESTS)
  add_test(NAME ${t} COMMAND ecu_tests ${t})
endforeach()

# CLI contract smoke tests (exit codes per docs/interfaces.md).
add_test(NAME cli_help COMMAND ecu_agent --help)
add_test(NAME cli_bad_args COMMAND ecu_agent --id ECU-X --hz notanumber)
set_tests_properties(cli_bad_args PROPERTIES WILL_FAIL TRUE)
add_test(NAME cli_missing_id COMMAND ecu_agent --hz 5)
set_tests_properties(cli_missing_id PROPERTIES WILL_FAIL TRUE)
add_test(NAME cli_bad_fault COMMAND ecu_agent --id ECU-X --fault BOGUS@1:2)
set_tests_properties(cli_bad_fault PROPERTIES WILL_FAIL TRUE)
# Runs for 1 s against nothing listening on high ports; must exit 0.
add_test(NAME agent_runs_offline COMMAND ecu_agent --id ECU-CTEST --duration 1 --hz 20
         --udp-port 48781 --tcp-port 48782 --fault OVERTEMP@0:1)
