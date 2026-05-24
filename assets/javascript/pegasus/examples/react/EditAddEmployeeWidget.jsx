import React, {useState} from "react";
import {Link, useNavigate} from "react-router-dom";
import ValidationErrors from "../../../utilities/ValidationErrors";
import {DepartmentEnum} from "api-client";



const EditAddEmployeeWidget = function(props) {
  const firstKey = Object.keys(DepartmentEnum)[0];
  const firstValue = DepartmentEnum[firstKey];
  const client = props.client;
  const [id, setId] = useState(props.id || null);
  const [name, setName] = useState(props.name || '');
  const [department, setDepartment] = useState(props.department || firstValue);
  const [salary, setSalary] = useState(props.salary || '');
  const [errors, setErrors] = useState({});
  const editMode = Boolean(id);
  const navigate = useNavigate();


  const saveEmployee = function() {
    let employee = {
      name: name,
      department: department,
      salary: salary,
    };
    let params = {};
    let apiFunction;
    if (editMode) {
      params['id'] = id;
      params['patchedEmployee'] = employee;
      apiFunction = 'employeesPartialUpdate';
    } else {
      params['employee'] = employee;
      apiFunction = 'employeesCreate';
    }
    client[apiFunction](params).then((result) => {
      props.employeeSaved(result);
      navigate(props.urlBase);
    }).catch((error) => {
      error.response.json().then((errors) => {
        setErrors(errors);
      })
    });
  };

  return (
    <section className="app-card">
      <h3 className="text-xl mb-1">Employee Details</h3>
      <div className="mb-3">
        <label className="block font-bold">Name</label>
        <input className="input w-full" type="text" placeholder="Michael Scott"
               onChange={(event) => setName(event.target.value)} value={name}>
        </input>
        <p className="text-sm text-base-content/70">Your employee's name.</p>
        <ValidationErrors errors={errors.name} />
      </div>
      <div className="mb-3">
        <label className="block font-bold">Department</label>
        <div className="pg-select">
          <select onChange={(event) => setDepartment(event.target.value)} value={department}>
            {Object.entries(DepartmentEnum).map(
              ([key, value], index) => <option key={value}
                                             value={value}>{key}</option>
            )}
          </select>
        </div>
        <p className="text-sm text-base-content/70">What department your employee belongs to.</p>
        <ValidationErrors errors={errors.department} />
      </div>
      <div className="mb-3">
        <label className="block font-bold">Salary</label>
        <input className="input w-full" type="number" min="0" placeholder="50000"
               onChange={(event) => setSalary(event.target.value)} value={salary}>
        </input>
        <p className="text-sm text-base-content/70">Your employee's annual salary.</p>
        <ValidationErrors errors={errors.salary} />
      </div>
      <div className="flex space-x-1">
        <button className={editMode ? 'btn btn-outline' : 'btn btn-primary'}
                onClick={() => saveEmployee()}>
            <span className="w-6 h-6 inline-flex justify-center items-center">
              <i className={`fa ${editMode ? 'fa-check' : 'fa-plus'}`}></i>
            </span>
          <span>{editMode ? 'Save Employee' : 'Add Employee'}</span>
        </button>
        <Link to={props.urlBase}>
          <button className="btn btn-ghost mx-2">
            <span>Cancel</span>
          </button>
        </Link>
      </div>
    </section>
  );
};

export default EditAddEmployeeWidget;
