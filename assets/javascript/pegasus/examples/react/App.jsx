import React, {useState, useEffect} from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  Link,
  useParams, useRoutes,
} from "react-router-dom";
import EditAddEmployeeWidget from "./EditAddEmployeeWidget";
import EmployeeTableRow from "./EmployeeTableRow";
import LoadingScreen from "../../../utilities/Loading";
import LoadError from "../../../utilities/LoadError";


const EmptyEmployeeList = function(props) {
  return (
    <section className="app-card">
      <div className="flex flex-col space-y-4 lg:flex-row lg:space-x-4 lg:space-y-0">
        <div className="basis-1/3 grow-0 shrink-0">
          <img className="img-fluid" alt="Nothing Here" src={props.emptyImage}/>
        </div>
        <div className="flex-1">
          <h1 className="text-3xl font-bold mb-2">No Employees Yet!</h1>
          <h2 className="text-xl mb-1">Create your first employee below to get started.</h2>
          <div className="my-3">
            <Link to="new">
              <button className="btn btn-primary">
                <span className="w-6 h-6 inline-flex justify-center items-center"><i className="fa fa-plus"></i></span>
                <span>Create Employee</span>
              </button>
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
};

const EmployeeList = function(props) {
  return (
    <section className="app-card">
      <h3 className="text-xl mb-1">All Employees</h3>
      <div className='table-responsive'>
        <table className="table table table-zebra w-full">
          <thead>
          <tr>
            <th className="text-left">Name</th>
            <th className="text-left">Department</th>
            <th className="text-right">Salary</th>
            <th></th>
          </tr>
          </thead>
          <tbody>
          {
            props.employees.map((employee, index) => {
              // https://stackoverflow.com/a/27009534/8207
              return <EmployeeTableRow key={employee.id} index={index} {...employee}
                                       delete={(index) => props.deleteEmployee(index)}
              />;
            })
          }
          </tbody>
        </table>
      </div>
      <Link to="new">
        <button className="btn btn-primary">
          <span className="w-6 h-6 inline-flex justify-center items-center">
            <i className="fa fa-plus"></i>
          </span>
          <span>Add Employee</span>
        </button>
      </Link>
    </section>
  );
};


const EmployeeApplication = function(props) {
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [employees, setEmployees] = useState([]);
  const client = props.client;

  useEffect(() => {
    client.employeesList().then((result) => {
      setEmployees(result.results);
      setIsLoading(false);
    }).catch((error) => {
      console.error(error);
      setLoadError(true);
      setIsLoading(false);
    });
  }, []);

  const getEmployeeById = function(id) {
    for (const employee of employees) {
      if (employee.id.toString() === id) {
        return employee;
      }
    }
  };

  const handleEmployeeSaved = function(employee) {
    let found = false;
    const newEmployees = [];
    // find the appropriate item in the list and update in place
    for (let existingEmployee of employees) {
      if (existingEmployee.id === employee.id) {
        newEmployees.push(employee);
        found = true;
      } else {
        newEmployees.push(existingEmployee);
      }
    }
    if (!found) {
      newEmployees.push(employee);
    }
    setEmployees(newEmployees);
  };

  const deleteEmployee = function (index) {
    client.employeesDestroy({id: employees[index].id}).then((result) => {
      const newEmployees = employees.slice(0, index).concat(employees.slice(index + 1));
      setEmployees(newEmployees);
    });
  };

  const RenderEditEmployee = function () {
    let params = useParams();
    if (isLoading) {
      return <LoadingScreen />;
    } else {
      const employeeId = params.employeeId;
      const employee = getEmployeeById(employeeId);
      return (
        <EditAddEmployeeWidget client={client} urlBase={props.urlBase} {...employee} employeeSaved={handleEmployeeSaved}/>
      );
    }
  };

  const getDefaultView = function() {
    if (isLoading) {
      return <LoadingScreen/>
    }
    if (loadError) {
      return <LoadError/>
    }
    if (employees.length === 0) {
      return <EmptyEmployeeList emptyImage={props.emptyImage}/>;
    } else {
      return <EmployeeList employees={employees} deleteEmployee={deleteEmployee}/>
    }
  };
  return (
    <Routes>
      <Route path="" element={getDefaultView()} />
      <Route path="new" element={<EditAddEmployeeWidget client={client} employeeSaved={handleEmployeeSaved} urlBase={props.urlBase}/>} />
      <Route path="edit/:employeeId" element={<RenderEditEmployee />} />
    </Routes>
  );
};


export default EmployeeApplication;
